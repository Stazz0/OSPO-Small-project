#!/usr/bin/python3

from rocrate.rocrate import ROCrate
import json
import os

def analyze_ro_crate(RO_CRATE_PATH):
    crate = ROCrate(RO_CRATE_PATH)

    with open(os.path.join(RO_CRATE_PATH, 'ro-crate-metadata.json'), 'r') as f:
        metadata = json.load(f)

    dependencies = metadata.get('softwareDependencies', [])
    os_requirements = metadata.get('operatingSystem', None)

    print("Software Dependencies:")
    for dep in dependencies:
        print(dep['name'], dep.get('version', 'Unspecified'))

    print("Required Operating System :")
    if os_requirements:
        print(os_requirements)
    else:
        print("Required Operating system is not specified")

    authors = crate.creator
    print("Authors:")
    for author in authors:
        print(author.name)

if __name__ == "__main__":
    ro_crate_path = os.path.expanduser('~/OSPO-Gsoc/OSPO-Small-project/RO-Crate')
    analyze_ro_crate(ro_crate_path)

