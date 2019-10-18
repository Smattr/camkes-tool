#!/usr/bin/env python
# -*- coding: utf-8 -*-

#
# Copyright 2017, Data61
# Commonwealth Scientific and Industrial Research Organisation (CSIRO)
# ABN 41 687 119 230.
#
# This software may be distributed and modified according to the terms of
# the BSD 2-Clause license. Note that NO WARRANTY is provided.
# See "LICENSE_BSD2.txt" for details.
#
# @TAG(DATA61_BSD)
#

# Macros for use inside the templates.

from __future__ import absolute_import, division, print_function, \
    unicode_literals
from camkes.internal.seven import cmp, filter, map, zip

from camkes.ast import Composition, Instance, Parameter, Struct
from camkes.templates import sizeof_probe, TemplateError
from capdl import ASIDPool, CNode, Endpoint, Frame, IODevice, IOPageTable, \
    Notification, page_sizes, PageDirectory, PageTable, TCB, Untyped, \
    calculate_cnode_size, lookup_architecture
import collections
import math
import os
import platform
import re
import six

from camkes.templates.arch_helpers import min_untyped_size, max_untyped_size

# Exclusions for the used macros test (see testmacros.py)
NO_CHECK_UNUSED = set()


def generated_file_notice():
    return 'THIS FILE IS AUTOMATICALLY GENERATED. YOUR EDITS WILL BE OVERWRITTEN.'


def thread_stack(sym, size):
    return 'char %s[ROUND_UP_UNSAFE(%d, ' \
        'PAGE_SIZE_4K) + PAGE_SIZE_4K * 2]\n' \
           '    VISIBLE\n' \
           '    __attribute__((section("align_12bit")))\n' \
           '    ALIGN(PAGE_SIZE_4K);\n' % (sym, size)


def ipc_buffer(sym):
    return 'char %s[PAGE_SIZE_4K * 3]\n' \
           '    VISIBLE\n' \
           '    __attribute__((section("align_12bit")))\n' \
           '    ALIGN(PAGE_SIZE_4K);\n' % sym


def ipc_buffer_address(sym):
    return '((seL4_Word)%s + PAGE_SIZE_4K);\n' % sym


def next_page_multiple(size, arch):
    '''
    Finds the smallest multiple of 4K that can comfortably be used to create
    a mapping for the provided size on a given architecture.
    '''
    multiple = page_sizes(arch)[0]
    while size > multiple:
        multiple += 4096
    return multiple


# Python 2 type annotations
next_page_multiple.__annotations__ = {'size': int, 'arch': str, 'return': int}


def align_page_address(address, arch):
    page_size = page_sizes(arch)[0]
    return address & ~(page_size-1)


def get_page_size(size, arch):
    '''
    Returns the largest frame_size that can be used to create
    a mapping for the provided size on a given architecture. It assumes
    that the variable will be aligned to the start of the frame.
    '''
    frame_size = 0
    size = int(size)
    for sz in reversed(page_sizes(arch)):
        if size >= sz and size % sz == 0:
            frame_size = sz
            break
    return frame_size


def get_perm(configuration, instance_name, interface_name):
    '''Fetch a valid permission string'''
    perm = configuration[instance_name].get('%s_access' % interface_name)
    if not perm:
        perm = "RWXP"
    elif not re.match('^R?W?X?P?$', perm):
        raise(TemplateError('invalid permissions attribute %s.%s_access' %
                            (instance_name, interface_name)))
    return perm


def show_type(t):
    assert isinstance(t, (six.string_types, Struct))
    if isinstance(t, six.string_types):
        if t == 'string':
            return 'char *'
        elif t == 'character':
            return 'char'
        elif t == 'boolean':
            return 'bool'
        else:
            return t
    else:
        return "struct " + t.name


def type_to_fit_integer(value):
    assert isinstance(value, six.integer_types)
    if value <= 2 ** 8:
        return 'uint8_t'
    elif value <= 2 ** 16:
        return 'uint16_t'
    elif value <= 2 ** 32:
        return 'uint32_t'
    elif value <= 2 ** 64:
        return 'uint64_t'
    else:
        raise Exception('No type to fit value %s' % value)


def print_type_definitions(attributes, values):
    def print_struct_definition(struct, sub_value):
        return_string = "struct %s {\n" % struct.name
        for i in struct.attributes:
            array_string = ""
            if i.array:
                array_string = "[%d]" % (len(sub_value.get(i.name)) if sub_value else 0)
            return_string += "%s %s%s;\n" % (show_type(i.type), i.name, array_string)
        return return_string + "};\n"

    def recurse_structs(attribute, values):
        struct = attribute.type
        structs = []
        for sub_attribute in struct.attributes:
            if isinstance(sub_attribute.type, Struct):
                structs.extend(recurse_structs(sub_attribute, values.get(sub_attribute.name)))

        if attribute.array:
            values = values[0] if values else None
        structs.append((struct, values))
        return structs

    return_string = ""
    structs = []
    for attribute in attributes:
        if isinstance(attribute.type, Struct):
            structs.extend(recurse_structs(attribute, values.get(attribute.name)))

    already_drawn = dict()
    for (struct, sub_value) in structs:
        if struct.name not in already_drawn:
            return_string += str(print_struct_definition(struct, sub_value))
            already_drawn[struct.name] = 1

    return return_string


def show_attribute_value(t, value):
    """ Prints out an attributes value.
        An attriubte can be an array (although this is provided to the template as a tuple type)
        An attribute can also be a camkes structure which is a dictionary of attributes (keys) with corresponding values
    """
    return_string = ""
    is_array = False
    if isinstance(value, (tuple, list)):
        is_array = True
        values = value
        return_string += "{\n"
    else:
        values = (value,)

    # runs for every element in the array, if a non array attribute then this just runs once.
    for i, value in enumerate(values):
        if isinstance(value, six.string_types):  # For string literals
            return_string += "\"%s\"" % value
        elif isinstance(t.type, Struct):  # For struct attributes (This recursively calls this function)
            return_string += "{\n"
            for attribute in t.type.attributes:
                return_string += "." + str(attribute.name)  # + ("[]" if attribute.array else "")
                return_string += " = " + \
                    str(show_attribute_value(attribute, value[attribute.name])) + ",\n"
            return_string += "}"
        else:  # For all other literal types
            return_string += "%s" % str(value)

        # Add comma if element is part of an array
        if i < (len(values)-1):
            return_string += ",\n"
    if is_array:
        return_string += "}"
    return return_string


def show_includes(xs, prefix=''):
    s = ''
    for header in xs:
        if header.relative:
            s += '#include "%(prefix)s%(source)s"\n' % {
                'prefix': prefix,
                'source': header.source,
            }
        else:
            s += '#include <%s>\n' % header.source
    return s


PAGE_SIZE = 4096


def threads(composition, instance, configuration, options):
    '''
    Compute the threads for a given instance.

    This function returns an array of all the threads for a component
    containing properties for each thread:
    - name: The name used for creating objects
    - interface: If the thread is an interface thread, what interface it is
    for
    - intra_index: Index of the thread within an interface that has more
    than one thread. 0 if not an interface thread.
    - stack_symbol: Name of the stack for this thread
    - stack_size: Size of the stack
    - ipc_symbol: Name of the ipc buffer symbol for this thread.    '''
    assert isinstance(composition, Composition)
    assert isinstance(instance, Instance)

    class Thread(object):
        def __init__(self, name, interface, intra_index, stack_size):
            self.name = name
            self.interface = interface
            self.intra_index = intra_index
            self.stack_symbol = "_camkes_stack_%s" % name
            self.stack_size = stack_size
            self.ipc_symbol = "_camkes_ipc_buffer_%s" % name
            self.sp = "get_vaddr(\'%s\') + %d" % (self.stack_symbol, self.stack_size + PAGE_SIZE)
            self.addr = "get_vaddr(\'%s\') + %d" % (self.ipc_symbol, PAGE_SIZE)

    instance_name = re.sub(r'[^A-Za-z0-9]', '_', instance.name)
    # First thread is control thread
    stack_size = configuration.get('_stack_size', options.default_stack_size)
    name = "%s_0_control" % instance_name
    ts = [Thread(name, None, 0, stack_size)]
    for connection in composition.connections:
        for end in connection.from_ends:
            if end.instance == instance:
                for x in six.moves.range(connection.type.from_threads):
                    name = "%s_%s_%04d" % (instance_name, end.interface.name, x)
                    stack_size = configuration.get('%s_stack_size' %
                                                   end.interface.name, options.default_stack_size)
                    ts.append(Thread(name, end.interface, x, stack_size))
        for end in connection.to_ends:
            if end.instance == instance:
                for x in six.moves.range(connection.type.to_threads):
                    name = "%s_%s_%04d" % (instance_name, end.interface.name, x)
                    stack_size = configuration.get('%s_stack_size' %
                                                   end.interface.name, options.default_stack_size)
                    ts.append(Thread(name, end.interface, x, stack_size))

    if options.debug_fault_handlers:
        # Last thread is fault handler thread
        stack_size = options.default_stack_size
        name = "%s_0_fault_handler" % instance_name
        ts.append(Thread(name, None, 0, stack_size))
    return ts


def dataport_size(type):
    assert isinstance(type, six.string_types)
    m = re.match(r'Buf\((\d+)\)$', type)
    if m is not None:
        return m.group(1)
    return 'sizeof(%s)' % show_type(type)


def dataport_type(type):
    assert isinstance(type, six.string_types)
    if re.match(r'Buf\((\d+)\)$', type) is not None:
        return 'void'
    return show_type(type)


# The following macros are for when you require generation-time constant
# folding. These are not robust and for cases when a generation-time constant
# is not required, you should simply emit the C equivalent and let the C
# compiler handle it.

def ROUND_UP(x, y):
    assert isinstance(x, six.integer_types)
    assert isinstance(y, six.integer_types) and y > 0
    return (x + y - 1) // y * y


def ROUND_DOWN(x, y):
    assert isinstance(x, six.integer_types)
    assert isinstance(y, six.integer_types) and y > 0
    return x // y * y

# This macro is currently only used outside the repository
NO_CHECK_UNUSED.add('ROUND_DOWN')


_sizes = {
    # The sizes of a few things we know statically.
    'Buf': 4096,
    'int8_t': 1,
    'uint8_t': 1,
    'int16_t': 2,
    'uint16_t': 2,
    'int32_t': 4,
    'uint32_t': 4,
    'int64_t': 8,
    'uint64_t': 8,
}


def sizeof(arch, t):
    assert isinstance(t, (Parameter,) + six.string_types)

    if isinstance(t, Parameter):
        return sizeof(arch, t.type)

    size = _sizes.get(t)
    if size is None:
        # We don't know the size of this type, so we'll ask the C compiler.
        toolprefix = os.environ.get('TOOLPREFIX', '')
        compiler = '%sgcc' % toolprefix

        extra_flags = []
        # Account for the fact that we may be cross-compiling using our native
        # compiler.
        if arch == 'ia32' and platform.machine() == 'x86_64':
            extra_flags.append('-m32')
        elif arch == 'x86_64' and platform.machine() == 'i386':
            extra_flags.append('-m64')

        # Determine the size by invoking the c compiler
        size = sizeof_probe.probe_sizeof(t, compiler, extra_flags)

        # Cache the result for next time.
        _sizes[t] = size

    assert size is not None
    return size


def get_word_size(arch):
    return int(lookup_architecture(arch).word_size_bits()/8)


def maybe_set_property_from_configuration(configuration, prefix, obj, field_name, general_attribute):
    """Sets a field "field_name" of an object "obj" to the value of a configuration
    setting of the form:
    instance.attribute = value;
    where configuration is the configuration only for the instance.
    and "attribute" is obtained from the "general_attribute" and "prefix"
    If such a setting exists, the field is set.
    Otherwise, check if a corresponding general property was set for the instance.
    This is a setting that applies the property to all threads related to the instance
    including all interface threads."""

    attribute = prefix + general_attribute
    value = configuration.get(attribute)
    if value is None:
        general_value = configuration.get(general_attribute)
        if general_value is not None:
            setattr(obj, field_name, general_value)
    else:
        setattr(obj, field_name, value)

# This is just an internal helper
NO_CHECK_UNUSED.add('maybe_set_property_from_configuration')


def set_tcb_properties(tcb, options, configuration, prefix):
    tcb.prio = options.default_priority
    tcb.max_prio = options.default_max_priority
    tcb.affinity = options.default_affinity

    maybe_set_property_from_configuration(configuration, prefix, tcb, 'prio', 'priority')
    maybe_set_property_from_configuration(configuration, prefix, tcb, 'max_prio', 'max_priority')
    maybe_set_property_from_configuration(configuration, prefix, tcb, 'affinity', 'affinity')
    # Find the domain if it was set.
    dom_attribute = prefix + "domain"
    dom = configuration.get(dom_attribute)

    if dom is not None:
        tcb.domain = dom


def set_sc_properties(sc, options, configuration, prefix):
    sc.period = options.default_period
    sc.budget = options.default_budget
    sc.data = options.default_data
    sc.size_bits = options.default_size_bits

    maybe_set_property_from_configuration(configuration, prefix, sc, 'period', 'period')
    maybe_set_property_from_configuration(configuration, prefix, sc, 'budget', 'budget')
    maybe_set_property_from_configuration(configuration, prefix, sc, 'data', 'data')
    maybe_set_property_from_configuration(configuration, prefix, sc, 'size_bits', 'size_bits')


def check_isabelle_outfile(thy_name, outfile_name):
    '''Our Isabelle templates need to refer to each other using a
       consistent naming scheme. This checks that the expected theory
       name matches the output file passed on the command line.'''
    outfile_base = os.path.basename(outfile_name)
    if outfile_base.endswith('.thy'):
        outfile_base = outfile_base[:-len('.thy')]
    assert thy_name == outfile_base
    return ''

def isabelle_ident(n):
    '''Mangle the '.' in hierarchical object names.
       This should match the mangling performed by capDL-tool.'''
    assert re.match('^[a-zA-Z_](?:[a-zA-Z0-9_.]*[a-zA-Z0-9])?', n)
    return n.replace('.', '\'')

# This macro is only used as a base to define other functions (see Context)
NO_CHECK_UNUSED.add('isabelle_ident')

def isabelle_ADL_ident(type):
    '''Add a prefix for each object type. This helps to avoid name
       collisions for definitions generated from `arch-definitions.thy`.'''
    return lambda n: '{}__{}'.format(type, isabelle_ident(n))

# This macro is only used as a base to define other functions (see Context)
NO_CHECK_UNUSED.add('isabelle_ADL_ident')


def integrity_group_labels(composition, configuration):
    '''Given a CAmkES composition, return a dictionary that maps
       each non-independent instance and connection label to a
       canonical group label.

       In detail, this function maps:
       1. Grouped instances to the label of the group's composition.
       2. Components that have `integrity_label` configurations;
          the values are the group labels.
       3. Internal connections (from an instance label to itself) to
          their connected instances' labels.

       These instances and connections usually share cap rights in
       practice, and so cannot be proven to belong to separate
       integrity domains.
    '''
    group_labels = {}

    # Helper to compute transitive closure
    def get_canonical_label(l):
        group = l
        seen = [l]
        while group in group_labels:
            group = group_labels[group]
            if group in seen:
                raise TemplateError('cycle in group labelling: %s' %
                                    ', '.join(seen))
            seen.append(group)
        return group

    # 1. Groups
    # NB: the stage5 parser moves group information from composition.groups
    # into address space identifiers, so we need to look there instead.
    for c in composition.instances:
        if c.address_space != c.name:
            group_labels[c.name] = get_canonical_label(c.address_space)

    # 2. Direct configuration
    for c in composition.instances:
        l = configuration[c.name].get('integrity_label')
        if l is not None:
            group_labels[c.name] = get_canonical_label(l)

    # 3. Internal connections
    for conn in composition.connections:
        groups = set(get_canonical_label(end.instance.name)
                     for end in conn.from_ends + conn.to_ends)
        if len(groups) == 1:
            group_labels[conn.name] = list(groups)[0]

    return {
        c: get_canonical_label(group)
        for c, group in group_labels.items()
    }
