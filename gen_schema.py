#!/usr/bin/env python3
'''
Generate AVRO schema files from the service definition and textfsm files
'''

import sys
import os
import re
import yaml
import json
import ast
from pathlib import Path
from fastavro import parse_schema


def get_field_set(svc_def):
    '''Return the dict of fields with types given a service definition file'''

    field_set = set()
    defn_list = svc_def.get('apply', [])

    for entry in defn_list:
        nfn = defn_list[entry].get('normalize', None)
        if nfn:
            plist = re.split('''/(?=(?:[^'"]|'[^']*'|"[^"]*")*$)''', nfn)
            for elem in plist:
                try:
                    nfn_fld_list = ast.literal_eval(elem)
                    nfn_flds = [
                        re.match(r'\s*(\w+)', fld.split(':')[1]).group(1)
                        for fld in nfn_fld_list if type(fld) is str
                        ]
                    field_set.update(nfn_flds)
                except (ValueError, SyntaxError):
                    words = elem.split(':')
                    if len(words) > 1:
                        field_set.add(re.match(r'\s*(\w+)',
                                               words[1]).group(1))
        else:
            nfn = defn_list[entry].get('textfsm', None)
            if nfn:
                with open(nfn, 'r') as f:
                    lines = f.readlines()
                for line in lines:
                    if line.startswith('Value'):
                        field_set.add(re.match(r'^Value\s*(\S+)',
                                               line).group(1))

    return field_set


def write_avro_schema(name, fldset, keys, type, output_dir):
    '''Generate an AVRO schema file given a set of fields.
    We attempt to derive the type if we can. The logic followed for
    automatically deriving type is as follows:
    - if the field name ends in time/timestamp, type is float
    - if the field name ends in name, type is string
    - if the field name ends in list, type is array whose elements are string
    - if the field name ends in Cnt/Count, type is int
    '''
    schema = {
        "namespace": "suzieq",
        "name": name,
        "type": "record",
        "fields": []
    }

    for fld in fldset:
        field = {'name': fld}

        if fld.endswith('Cnt') or fld.endswith('Count'):
            field['type'] = 'long'
        elif fld.endswith('List'):
            field['type'] = {'type': 'array',
                             'items': {'type': 'string',
                                       'name': fld.split('List')[0]}}
        elif (fld.endswith('Time') or fld.endswith('Timestamp') or
              fld.endswith('time')):
            field['type'] = 'double'
        elif fld.endswith('name') or fld.endswith('Name'):
            field['type'] = 'string'
        elif type == 'counters':
            field['type'] = 'long'
        else:
            field['type'] = 'string'

        if fld in keys:
            field['key'] = True

        schema['fields'].append(field)
        field = {}

    # Add the default fields we add to every record
    schema['fields'].append({'name': 'hostname', 'type': 'string',
                             'key': True})
    schema['fields'].append({'name': 'timestamp', 'type': 'double'})
    schema['fields'].append({'name': 'active', 'type': 'boolean'})

    if parse_schema(schema):
        filename = output_dir + '/' + name + '.avsc'
        # We do require users to cleanup this auto-generated schema.
        # Don't make them do the work multiple times, save the original
        # type if defined, or default if not defined in the new schema
        if os.path.isfile(filename):
            with open(filename, 'r') as f:
                old_schema = json.loads(f.read())

            oldflds = {v['name']: i
                       for i, v in enumerate(old_schema['fields'])}
            newflds = {v['name']: i
                       for i, v in enumerate(schema['fields'])}

            for fld in oldflds.keys():
                if fld in newflds:
                    schema['fields'][newflds[fld]]['type'] = old_schema['fields'][oldflds[fld]]['type']
                    if (old_schema['fields'][oldflds[fld]].get('default', None)
                        and not schema['fields'][newflds[fld]].get('default',
                                                                   None)):
                        schema['fields'][newflds[fld]]['default'] = old_schema['fields'][oldflds[fld]]['default']

        with open(filename, 'w') as f:
            f.write(json.dumps(schema, indent=4))

    return


if __name__ == '__main__':

    if len(sys.argv) != 3:
        print('Usage: gen_schema <service file|service dir> <directory '
              'to write schema to')
        sys.exit(1)

    if not os.path.isdir(sys.argv[2]):
        print('{} must be a directory. Aborting', sys.argv[2])
        sys.exit(1)

    p = Path(sys.argv[1])
    if p.is_file():
        files = [p]
    elif p.is_dir():
        files = p.glob('*.yml')
    else:
        files = []

    for file in files:
        with file.open() as f:
            data = yaml.load(f.read())
            fldset = get_field_set(data)
            write_avro_schema(data['service'], fldset, data.get('keys', []),
                              data.get('type', None), sys.argv[2])