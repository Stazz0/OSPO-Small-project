#!/usr/bin/python

from rocrate.rocrate import ROCrate
from rocrate.model.person import Person
from rocrate.model.contextentity import ContextEntity
import json
import os

def analyze_ro_crate(rocrate_path):
    crate = ROCrate(rocrate_path)

    with open(os.path.join(rocrate_path, 'ro-crate-metadata.json'), 'r') as f:
        metadata = json.load(f)

    dependencies = metadata.get('softwareDependencies', [])
    os_requirements = metadata.get('operatingSystem', None)

    print("Software Dependencies:")
    for dep in dependencies:
        print(dep['name'], dep.get('version', 'Unspecified'))  # Improved output

    print("Operating System Requirements:")
    if os_requirements:
        print(os_requirements)
    else:
        print("Operating system requirements not explicitly specified")
