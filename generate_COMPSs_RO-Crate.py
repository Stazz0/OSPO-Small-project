#!/usr/bin/python
#
#  Copyright 2002-2023 Barcelona Supercomputing Center (www.bsc.es)
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""
    The generate_COMPSs_RO-Crate.py module generates the resulting RO-Crate metadata from a COMPSs application run
    following the Workflow Run Crate profile specification. Takes as parameters the ro-crate-info.yaml, and the
    dataprovenance.log generated from the run.
"""

from pathlib import Path
from urllib.parse import urlsplit
import os
import uuid
import typing
import datetime as dt
import json
import socket
import subprocess
import yaml
import time
import sys

from rocrate.rocrate import ROCrate
from rocrate.model.person import Person
from rocrate.model.contextentity import ContextEntity

# from rocrate.model.entity import Entity
# from rocrate.model.file import File
from rocrate.utils import iso_now

PROFILES_BASE = "https://w3id.org/ro/wfrun"
PROFILES_VERSION = "0.1"
WROC_PROFILE_VERSION = "1.0"


def fix_dir_url(in_url: str) -> str:
    """
    Fix dir:// URL returned by the runtime, change it to file:// and ensure it ends with '/'

    :param in_url: URL that may need to be fixed

    :returns: A file:// URL
    """

    runtime_url = urlsplit(in_url)
    if (
        runtime_url.scheme == "dir"
    ):  # Fix dir:// to file:// and ensure it ends with a slash
        new_url = "file://" + runtime_url.netloc + runtime_url.path
        if new_url[-1] != "/":
            new_url += "/"  # Add end slash if needed
        return new_url
    # else
    return in_url  # No changes required


def root_entity(compss_crate: ROCrate, yaml_content: dict) -> typing.Tuple[dict, list]:
    """
    Generate the Root Entity in the RO-Crate generated for the COMPSs application

    :param compss_crate: The COMPSs RO-Crate being generated
    :param yaml_content: Content of the YAML file specified by the user

    :returns: 'COMPSs Workflow Information' and 'Authors' sections, as defined in the YAML
    """

    # Get Sections
    compss_wf_info = yaml_content["COMPSs Workflow Information"]
    authors_info = []
    if "Authors" in yaml_content:
        authors_info_yaml = yaml_content["Authors"]  # Now a list of authors
        if isinstance(authors_info_yaml, list):
            authors_info = authors_info_yaml
        else:
            authors_info.append(authors_info_yaml)

    # COMPSs Workflow RO Crate generation
    # Root Entity
    compss_crate.name = compss_wf_info[
        "name"
    ]  # SHOULD in RO-Crate 1.1. MUST in WorkflowHub
    if "description" in compss_wf_info:
        compss_crate.description = compss_wf_info[
            "description"
        ]  # SHOULD in Workflow Profile and WorkflowHub
    if "license" in compss_wf_info:
        # License details could be also added as a Contextual Entity. MUST in Workflow RO-Crate Profile, but WorkflowHub does not consider it a mandatory field
        compss_crate.license = compss_wf_info["license"]

    author_list = []
    org_list = []

    for author in authors_info:
        properties_dict = {}
        if author["orcid"] not in author_list:
            # orcid is MANDATORY in RO-Crate 1.1
            author_list.append(author["orcid"])
        try:
            properties_dict["name"] = author["name"]  # MUST in WorkflowHub
        except KeyError:
            print(
                f"PROVENANCE | ERROR in your ro-crate-info.yaml file. Both 'orcid' and 'name' must be defined together for an Author"
            )
            raise
        if "ror" in author:
            # ror is not mandatory on any profile
            if author["ror"] not in org_list:
                org_list.append(author["ror"])
            properties_dict["affiliation"] = {"@id": author["ror"]}
            # If ror defined, organisation_name becomes mandatory, if it is to be shown in WorkflowHub
            try:
                compss_crate.add(
                    ContextEntity(
                        compss_crate,
                        author["ror"],
                        {"@type": "Organization", "name": author["organisation_name"]},
                    )
                )
            except KeyError:
                print(
                    f"PROVENANCE | ERROR in your ro-crate-info.yaml file. Both 'ror' and 'organisation_name' must be defined together for an Organisation"
                )
                raise
        if "e-mail" in author:
            properties_dict["contactPoint"] = {"@id": "mailto:" + author["e-mail"]}
            compss_crate.add(
                ContextEntity(
                    compss_crate,
                    "mailto:" + author["e-mail"],
                    {
                        "@type": "ContactPoint",
                        "contactType": "Author",
                        "email": author["e-mail"],
                        "identifier": author["e-mail"],
                        "url": author["orcid"],
                    },
                )
            )

        compss_crate.add(Person(compss_crate, author["orcid"], properties_dict))

    crate_author_list = []
    crate_org_list = []
    for author_orcid in author_list:
        crate_author_list.append({"@id": author_orcid})
    if crate_author_list:
        compss_crate.creator = crate_author_list
    for org_ror in org_list:
        crate_org_list.append({"@id": org_ror})

    # publisher is SHOULD in RO-Crate 1.1. Preferably an Organisation, but could be a Person
    if not crate_org_list:
        # Empty list of organisations, add authors as publishers
        if crate_author_list:
            compss_crate.publisher = crate_author_list
    else:
        compss_crate.publisher = crate_org_list

    return compss_wf_info, crate_author_list


def get_main_entities(wf_info: dict) -> typing.Tuple[str, str, str]:
    """
    Get COMPSs version and mainEntity from dataprovenance.log first lines
    3 First lines expected format: compss_version_number\n main_entity\n output_profile_file\n
    Next lines are for "accessed files" and "direction"
    mainEntity can be directly obtained for Python, or defined by the user in the YAML (sources_main_file)

    :param wf_info: YAML dict to extract info form the application, as specified by the user

    :returns: COMPSs version, main COMPSs file name, COMPSs profile file name
    """

    # Build the whole source files list in list_of_sources, and get a backup main entity, in case we can't find one
    # automatically. The mainEntity must be an existing file, otherwise the RO-Crate won't have a ComputationalWorkflow
    yaml_sources_list = []  # YAML sources list
    list_of_sources = []  # Full list of source files, once directories are traversed
    # Should contain absolute paths, for correct comparison (two files in different directories
    # could be named the same)

    main_entity = None
    backup_main_entity = None

    if "sources" in wf_info:
        if isinstance(wf_info["sources"], list):
            yaml_sources_list.extend(wf_info["sources"])
        else:
            yaml_sources_list.append(wf_info["sources"])
    if "files" in wf_info:
        # Backward compatibility: if old "sources_dir" and "files" have been used, merge in yaml_sources_list.
        if isinstance(wf_info["files"], list):
            yaml_sources_list.extend(wf_info["files"])
        else:
            yaml_sources_list.append(wf_info["files"])
    if "sources_dir" in wf_info:
        #  Backward compatibility: if old "sources_dir" and "files" have been used, merge in yaml_sources_list.
        # sources_list = list(tuple(wf_info["files"])) + list(tuple(wf_info["sources"]))
        if isinstance(wf_info["sources_dir"], list):
            yaml_sources_list.extend(wf_info["sources_dir"])
        else:
            yaml_sources_list.append(wf_info["sources_dir"])

    keys = ["sources", "files", "sources_dir"]
    if not any(key in wf_info for key in keys):
        # If no sources are defined, define automatically the main_entity or return error
        # We try directly to add the mainEntity identified in dataprovenance.log, if exists in the CWD
        with open(DP_LOG, "r", encoding="UTF-8") as dp_file:
            compss_v = next(dp_file).rstrip()  # First line, COMPSs version number
            second_line = next(dp_file).rstrip()
            # Second, main_entity. Use better rstrip, just in case there is no '\n'
            if second_line.endswith(".py"):
                # Python. Line contains only the file name, need to locate it
                detected_app = second_line
            else:  # Java app. Need to fix filename first
                # Translate identified main entity matmul.files.Matmul to a comparable path
                me_file_name = second_line.split(".")[-1]
                detected_app = me_file_name + ".java"
            # print(f"PROVENANCE DEBUG | Detected app when no 'sources' defined is: {detected_app}")
            third_line = next(dp_file).rstrip()
            out_profile_fn = Path(third_line)
        if os.path.isfile(detected_app):
            main_entity = detected_app
        else:
            print(
                f"PROVENANCE | ERROR: No 'sources' defined at ro-crate-info.yaml, and detected mainEntity not found in Current Working Directory"
            )
            raise KeyError("No 'sources' key defined at ro-crate-info.yaml")

    # Find a backup_main_entity while building the full list of source files
    for source in yaml_sources_list:
        path_source = Path(source).expanduser()
        resolved_source = str(path_source.resolve())
        if path_source.exists():
            if os.path.isfile(resolved_source):
                list_of_sources.append(resolved_source)
                if backup_main_entity is None and path_source.suffix in {
                    ".py",
                    ".java",
                    ".jar",
                    ".class",
                }:
                    backup_main_entity = resolved_source
                    # print(
                    #     f"PROVENANCE DEBUG | FOUND SOURCE FILE AS BACKUP MAIN: {backup_main_entity}"
                    # )
            elif os.path.isdir(resolved_source):
                for root, _, files in os.walk(
                    resolved_source, topdown=True, followlinks=True
                ):
                    if "__pycache__" in root:
                        continue  # We skip __pycache__ subdirectories
                    for f_name in files:
                        # print(f"PROVENANCE DEBUG | ADDING FILE to list_of_sources: {f_name}. root is: {root}")
                        if f_name.startswith("*"):
                            # Avoid dealing with symlinks with wildcards
                            continue
                        full_name = os.path.join(root, f_name)
                        list_of_sources.append(full_name)
                        if backup_main_entity is None and Path(f_name).suffix in {
                            ".py",
                            ".java",
                            ".jar",
                            ".class",
                        }:
                            backup_main_entity = full_name
                            # print(
                            #     f"PROVENANCE DEBUG | FOUND SOURCE FILE IN A DIRECTORY AS BACKUP MAIN: {backup_main_entity}"
                            # )
            else:
                print(
                    f"PROVENANCE | WARNING: A defined source is neither a directory, nor a file ({resolved_source})"
                )
        else:
            print(
                f"PROVENANCE | WARNING: Specified file or directory in ro-crate-info.yaml 'sources' does not exist ({path_source})"
            )

    # Can't get backup_main_entity from sources_main_file, because we do not know if it really exists
    if len(list_of_sources) == 0:
        print(
            "PROVENANCE | WARNING: Unable to find application source files. Please, review your "
            "ro_crate_info.yaml definition ('sources' term)"
        )
        # raise FileNotFoundError
    elif backup_main_entity is None:
        # No source files found in list_of_sources, set any file as backup
        backup_main_entity = list_of_sources[0]

    # print(f"PROVENANCE DEBUG | backup_main_entity is: {backup_main_entity}")

    with open(DP_LOG, "r", encoding="UTF-8") as dp_file:
        compss_v = next(dp_file).rstrip()  # First line, COMPSs version number
        second_line = next(dp_file).rstrip()
        # Second, main_entity. Use better rstrip, just in case there is no '\n'
        if second_line.endswith(".py"):
            # Python. Line contains only the file name, need to locate it
            detected_app = second_line
        else:  # Java app. Need to fix filename first
            # Translate identified main entity matmul.files.Matmul to a comparable path
            me_sub_path = second_line.replace(".", "/")
            detected_app = me_sub_path + ".java"
        # print(f"PROVENANCE DEBUG | Detected app is: {detected_app}")
        third_line = next(dp_file).rstrip()
        out_profile_fn = Path(third_line)

    for file in list_of_sources:  # Try to find the identified mainEntity
        if file.endswith(detected_app):
            # print(
            #     f"PROVENANCE DEBUG | IDENTIFIED MAIN ENTITY FOUND IN LIST OF FILES: {file}"
            # )
            main_entity = file
            break
    # main_entity has a value if mainEntity has been automatically detected

    if "sources_main_file" in wf_info:
        # Check what the user has defined
        # If it directly exists, we are done, no need to search in 'sources'
        found = False
        path_smf = Path(wf_info["sources_main_file"]).expanduser()
        resolved_sources_main_file = str(path_smf.resolve())
        if os.path.isfile(path_smf):
            # Checks if exists
            if main_entity is None:
                # the detected_app was not found previously in the list of files
                found = True
                print(
                    f"PROVENANCE | WARNING: The file defined at sources_main_file is assigned as mainEntity: {resolved_sources_main_file}"
                )
            else:
                print(
                    f"PROVENANCE | WARNING: The file defined at sources_main_file "
                    f"({resolved_sources_main_file}) in ro-crate-info.yaml does not match with the "
                    f"automatically identified mainEntity ({main_entity})"
                )
            main_entity = resolved_sources_main_file
            found = True
        else:
            # If the file defined in sources_main_file is not directly found, try to find it in 'sources'
            # if sources_main_file is an absolute path, the join has no effect
            for source in yaml_sources_list:  # Created at the beginning
                path_sources = Path(source).expanduser()
                if not path_sources.exists() or os.path.isfile(source):
                    continue
                resolved_sources = str(path_sources.resolve())
                resolved_sources_main_file = os.path.join(
                    resolved_sources, wf_info["sources_main_file"]
                )
                for file in list_of_sources:
                    if file == resolved_sources_main_file:
                        # The file exists
                        # print(
                        #     f"PROVENANCE DEBUG | The file defined at sources_main_file exists: "
                        #     f" {resolved_sources_main_file}"
                        # )
                        if resolved_sources_main_file != main_entity:
                            print(
                                f"PROVENANCE | WARNING: The file defined at sources_main_file "
                                f"({resolved_sources_main_file}) in ro-crate-info.yaml does not match with the "
                                f"automatically identified mainEntity ({main_entity})"
                            )
                        # else: the user has defined exactly the file we found
                        # In both cases: set file defined by user
                        main_entity = resolved_sources_main_file
                        # Can't use Path, file may not be in cwd
                        found = True
                        break
                    if file.endswith(wf_info["sources_main_file"]):
                        # The file exists
                        # print(
                        #     f"PROVENANCE DEBUG | The file defined at sources_main_file exists: "
                        #     f" {resolved_sources_main_file}"
                        # )
                        if file != main_entity:
                            print(
                                f"PROVENANCE | WARNING: The file defined at sources_main_file "
                                f"({file}) in ro-crate-info.yaml does not match with the "
                                f"automatically identified mainEntity ({main_entity})"
                            )
                        # else: the user has defined exactly the file we found
                        # In both cases: set file defined by user
                        main_entity = file
                        # Can't use Path, file may not be in cwd
                        found = True
                        break
            if not found:
                print(
                    f"PROVENANCE | WARNING: the defined 'sources_main_file' ({wf_info['sources_main_file']}) does "
                    f"not exist in the defined 'sources'. Check your ro-crate-info.yaml."
                )
                # If we identified the mainEntity automatically, we select it when the one defined
                # by the user is not found

    if main_entity is None:
        # When neither identified, nor defined by user: get backup if exists
        if backup_main_entity is None:
            # We have a fatal problem
            print(
                f"PROVENANCE | ERROR: no mainEntity has been found. Check the definition of 'sources' and "
                f"'sources_main_file' in ro-crate-info.yaml"
            )
            raise FileNotFoundError
        main_entity = backup_main_entity
        print(
            f"PROVENANCE | WARNING: the detected mainEntity {detected_app} does not exist in the list "
            f"of application files provided in ro-crate-info.yaml. Setting {main_entity} as mainEntity"
        )

    print(
        f"PROVENANCE | COMPSs version: {compss_v}, out_profile: {out_profile_fn.name}, main_entity: {main_entity}"
    )

    return compss_v, main_entity, out_profile_fn.name


def process_accessed_files() -> typing.Tuple[list, list]:
    """
    Process all the files the COMPSs workflow has accessed. They will be the overall inputs needed and outputs
    generated of the whole workflow.
    - If a task that is an INPUT, was previously an OUTPUT, it means it is an intermediate file, therefore we discard it
    - Works fine with COLLECTION_FILE_IN, COLLECTION_FILE_OUT and COLLECTION_FILE_INOUT

    :returns: List of Inputs and Outputs of the COMPSs workflow
    """

    part_time = time.time()

    inputs = set()
    outputs = set()

    with open(DP_LOG, "r", encoding="UTF-8") as dp_file:
        for line in dp_file:
            file_record = line.rstrip().split(" ")
            if len(file_record) == 2:
                if (
                    file_record[1] == "IN" or file_record[1] == "IN_DELETE"
                ):  # Can we have an IN_DELETE that was not previously an OUTPUT?
                    if (
                        file_record[0] not in outputs
                    ):  # A true INPUT, not an intermediate file
                        inputs.add(file_record[0])
                    #  Else, it is an intermediate file, not a true INPUT or OUTPUT. Not adding it as an input may
                    # be enough in most cases, since removing it as an output may be a bit radical
                    #     outputs.remove(file_record[0])
                elif file_record[1] == "OUT":
                    outputs.add(file_record[0])
                else:  # INOUT, COMMUTATIVE, CONCURRENT
                    if (
                        file_record[0] not in outputs
                    ):  # Not previously generated by another task (even a task using that same file), a true INPUT
                        inputs.add(file_record[0])
                    # else, we can't know for sure if it is an intermediate file, previous call using the INOUT may
                    # have inserted it at outputs, thus don't remove it from outputs
                    outputs.add(file_record[0])
            # else dismiss the line

    l_ins = list(inputs)
    l_ins.sort()  # Put directories first
    l_outs = list(outputs)
    l_outs.sort()  # Put directories first

    # Fix dir:// references, they don't end with slash '/' at dataprovenance.log
    for data_list in [l_ins, l_outs]:
        for item in data_list:
            url_parts = urlsplit(item)
            if url_parts.scheme == "dir":
                data_list.append("dir://" + socket.gethostname() + url_parts.path + "/")
                data_list.remove(item)
            else:
                break  # File has been reached, all directories have been treated
        data_list.sort()

    print(f"PROVENANCE | COMPSs runtime detected inputs ({len(l_ins)})")
    print(f"PROVENANCE | COMPSs runtime detected outputs ({len(l_outs)})")
    print(
        f"PROVENANCE | dataprovenance.log processing TIME: "
        f"{time.time() - part_time} s"
    )

    return l_ins, l_outs


def add_file_to_crate(
    compss_crate: ROCrate,
    file_name: str,
    compss_ver: str,
    main_entity: str,
    out_profile: str,
    in_sources_dir: str,
) -> str:
    """
    Get details of a file, and add it physically to the Crate. The file will be an application source file, so,
    the destination directory should be 'application_sources/'

    :param compss_crate: The COMPSs RO-Crate being generated
    :param file_name: File to be added physically to the Crate, full path resolved
    :param compss_ver: COMPSs version number
    :param main_entity: COMPSs file with the main code, full path resolved
    :param out_profile: COMPSs application profile output
    :param in_sources_dir: Path to the defined sources_dir. May be passed empty, so there is no sub-folder structure
        to be respected

    :returns: Path where the file has been stored in the crate
    """

    file_path = Path(file_name)
    file_properties = {
        "name": file_path.name,
        "contentSize": os.path.getsize(file_name),
    }

    # main_entity has its absolute path, as well as file_name
    if file_name == main_entity:
        file_properties["description"] = "Main file of the COMPSs workflow source files"
        if file_path.suffix == ".jar":
            file_properties["encodingFormat"] = (
                [
                    "application/java-archive",
                    {"@id": "https://www.nationalarchives.gov.uk/PRONOM/x-fmt/412"},
                ],
            )
            # Add JAR as ContextEntity
            compss_crate.add(
                ContextEntity(
                    compss_crate,
                    "https://www.nationalarchives.gov.uk/PRONOM/x-fmt/412",
                    {"@type": "WebSite", "name": "Java Archive Format"},
                )
            )
        elif file_path.suffix == ".class":
            file_properties["encodingFormat"] = (
                [
                    "application/java",
                    {"@id": "https://www.nationalarchives.gov.uk/PRONOM/x-fmt/415"},
                ],
            )
            # Add CLASS as ContextEntity
            compss_crate.add(
                ContextEntity(
                    compss_crate,
                    "https://www.nationalarchives.gov.uk/PRONOM/x-fmt/415",
                    {"@type": "WebSite", "name": "Java Compiled Object Code"},
                )
            )
        else:  # .py, .java, .c, .cc, .cpp
            file_properties["encodingFormat"] = "text/plain"
        if complete_graph.exists():
            file_properties["image"] = {
                "@id": "complete_graph.svg"
            }  # Name as generated

        # input and output properties not added to the workflow, since we do not comply with BioSchemas
        # (i.e. no FormalParameters are defined)

    else:
        # Any other extra file needed
        file_properties["description"] = "Auxiliary File"
        if file_path.suffix in (".py", ".java"):
            file_properties["encodingFormat"] = "text/plain"
            file_properties["@type"] = ["File", "SoftwareSourceCode"]
        elif file_path.suffix == ".json":
            file_properties["encodingFormat"] = [
                "application/json",
                {"@id": "https://www.nationalarchives.gov.uk/PRONOM/fmt/817"},
            ]
        elif file_path.suffix == ".pdf":
            file_properties["encodingFormat"] = (
                [
                    "application/pdf",
                    {"@id": "https://www.nationalarchives.gov.uk/PRONOM/fmt/276"},
                ],
            )
        elif file_path.suffix == ".svg":
            file_properties["encodingFormat"] = (
                [
                    "image/svg+xml",
                    {"@id": "https://www.nationalarchives.gov.uk/PRONOM/fmt/92"},
                ],
            )
        elif file_path.suffix == ".jar":
            file_properties["encodingFormat"] = (
                [
                    "application/java-archive",
                    {"@id": "https://www.nationalarchives.gov.uk/PRONOM/x-fmt/412"},
                ],
            )
            # Add JAR as ContextEntity
            compss_crate.add(
                ContextEntity(
                    compss_crate,
                    "https://www.nationalarchives.gov.uk/PRONOM/x-fmt/412",
                    {"@type": "WebSite", "name": "Java Archive Format"},
                )
            )
        elif file_path.suffix == ".class":
            file_properties["encodingFormat"] = (
                [
                    "Java .class",
                    {"@id": "https://www.nationalarchives.gov.uk/PRONOM/x-fmt/415"},
                ],
            )
            # Add CLASS as ContextEntity
            compss_crate.add(
                ContextEntity(
                    compss_crate,
                    "https://www.nationalarchives.gov.uk/PRONOM/x-fmt/415",
                    {"@type": "WebSite", "name": "Java Compiled Object Code"},
                )
            )

    # Build correct dest_path. If the file belongs to sources_dir, need to remove all "sources_dir" from file_name,
    # respecting the sub_dir structure.
    # If the file is defined individually, put in the root of application_sources

    if in_sources_dir:
        # /home/bsc/src/file.py must be translated to application_sources/src/file.py,
        # but in_sources_dir is /home/bsc/src
        new_root = str(Path(in_sources_dir).parents[0])
        final_name = file_name[len(new_root) + 1 :]
        path_in_crate = "application_sources/" + final_name
    else:
        path_in_crate = "application_sources/" + file_path.name

    if file_name != main_entity:
        # print(f"PROVENANCE DEBUG | Adding auxiliary source file: {file_name}")
        compss_crate.add_file(
            source=file_name, dest_path=path_in_crate, properties=file_properties
        )
    else:
        # We get lang_version from dataprovenance.log
        # print(f"PROVENANCE DEBUG | Adding main source file: {file_path.name}, file_name: {file_name}")
        compss_crate.add_workflow(
            source=file_name,
            dest_path=path_in_crate,
            main=True,
            lang="COMPSs",
            lang_version=compss_ver,
            properties=file_properties,
            gen_cwl=False,
        )

        # complete_graph.svg
        if complete_graph.exists():
            file_properties = {}
            file_properties["name"] = "complete_graph.svg"
            file_properties["contentSize"] = complete_graph.stat().st_size
            file_properties["@type"] = ["File", "ImageObject", "WorkflowSketch"]
            file_properties[
                "description"
            ] = "The graph diagram of the workflow, automatically generated by COMPSs runtime"
            # file_properties["encodingFormat"] = (
            #     [
            #         "application/pdf",
            #         {"@id": "https://www.nationalarchives.gov.uk/PRONOM/fmt/276"},
            #     ],
            # )
            file_properties["encodingFormat"] = (
                [
                    "image/svg+xml",
                    {"@id": "https://www.nationalarchives.gov.uk/PRONOM/fmt/92"},
                ],
            )
            file_properties["about"] = {
                "@id": path_in_crate
            }  # Must be main_entity_location, not main_entity alone
            # Add PDF as ContextEntity
            # compss_crate.add(
            #     ContextEntity(
            #         compss_crate,
            #         "https://www.nationalarchives.gov.uk/PRONOM/fmt/276",
            #         {
            #             "@type": "WebSite",
            #             "name": "Acrobat PDF 1.7 - Portable Document Format",
            #         },
            #     )
            # )
            compss_crate.add(
                ContextEntity(
                    compss_crate,
                    "https://www.nationalarchives.gov.uk/PRONOM/fmt/92",
                    {
                        "@type": "WebSite",
                        "name": "Scalable Vector Graphics",
                    },
                )
            )
            compss_crate.add_file(complete_graph, properties=file_properties)
        else:
            print(
                "PROVENANCE | WARNING: complete_graph.svg file not found. "
                "Provenance will be generated without image property"
            )

        # out_profile
        if os.path.exists(out_profile):
            file_properties = {}
            file_properties["name"] = out_profile
            file_properties["contentSize"] = os.path.getsize(out_profile)
            file_properties["description"] = "COMPSs application Tasks profile"
            file_properties["encodingFormat"] = [
                "application/json",
                {"@id": "https://www.nationalarchives.gov.uk/PRONOM/fmt/817"},
            ]

            # Fix COMPSs crappy format of JSON files
            with open(out_profile, encoding="UTF-8") as op_file:
                op_json = json.load(op_file)
            with open(out_profile, "w", encoding="UTF-8") as op_file:
                json.dump(op_json, op_file, indent=1)

            # Add JSON as ContextEntity
            compss_crate.add(
                ContextEntity(
                    compss_crate,
                    "https://www.nationalarchives.gov.uk/PRONOM/fmt/817",
                    {"@type": "WebSite", "name": "JSON Data Interchange Format"},
                )
            )
            compss_crate.add_file(out_profile, properties=file_properties)
        else:
            print(
                "PROVENANCE | WARNING: COMPSs application profile has not been generated. \
                  Make sure you use runcompss with --output_profile=file_name \
                  Provenance will be generated without profiling information"
            )

        # compss_submission_command_line.txt. Old compss_command_line_arguments.txt
        file_properties = {}
        file_properties["name"] = "compss_submission_command_line.txt"
        file_properties["contentSize"] = os.path.getsize(
            "compss_submission_command_line.txt"
        )
        file_properties[
            "description"
        ] = "COMPSs submission command line (runcompss / enqueue_compss), including flags and parameters passed to the application"
        file_properties["encodingFormat"] = "text/plain"
        compss_crate.add_file(
            "compss_submission_command_line.txt", properties=file_properties
        )

        # ro-crate-info.yaml
        file_properties = {}
        file_properties["name"] = "ro-crate-info.yaml"
        file_properties["contentSize"] = os.path.getsize("ro-crate-info.yaml")
        file_properties[
            "description"
        ] = "COMPSs Workflow Provenance YAML configuration file"
        file_properties["encodingFormat"] = [
            "YAML",
            {"@id": "https://www.nationalarchives.gov.uk/PRONOM/fmt/818"},
        ]

        # Add YAML as ContextEntity
        compss_crate.add(
            ContextEntity(
                compss_crate,
                "https://www.nationalarchives.gov.uk/PRONOM/fmt/818",
                {"@type": "WebSite", "name": "YAML"},
            )
        )
        compss_crate.add_file("ro-crate-info.yaml", properties=file_properties)

        return ""

    # print(f"ADDED FILE: {file_name} as {path_in_crate}")

    return path_in_crate


def add_application_source_files(
    compss_crate: ROCrate,
    compss_wf_info: dict,
    compss_ver: str,
    main_entity: str,
    out_profile: str,
) -> None:
    """
    Add all application source files as part of the crate. This means, to include them physically in the resulting
    bundle

    :param compss_crate: The COMPSs RO-Crate being generated
    :param compss_wf_info: YAML dict to extract info form the application, as specified by the user
    :param compss_ver: COMPSs version number
    :param main_entity: COMPSs file with the main code, full path resolved
    :param out_profile: COMPSs application profile output file

    :returns: None
    """

    part_time = time.time()

    sources_list = []

    if "sources" in compss_wf_info:
        if isinstance(compss_wf_info["sources"], list):
            sources_list.extend(compss_wf_info["sources"])
        else:
            sources_list.append(compss_wf_info["sources"])
    if "files" in compss_wf_info:
        # Backward compatibility: if old "sources_dir" and "files" have been used, merge in sources_list.
        if isinstance(compss_wf_info["files"], list):
            sources_list.extend(compss_wf_info["files"])
        else:
            sources_list.append(compss_wf_info["files"])
    if "sources_dir" in compss_wf_info:
        #  Backward compatibility: if old "sources_dir" and "files" have been used, merge in sources_list.
        # sources_list = list(tuple(wf_info["files"])) + list(tuple(wf_info["sources"]))
        if isinstance(compss_wf_info["sources_dir"], list):
            sources_list.extend(compss_wf_info["sources_dir"])
        else:
            sources_list.append(compss_wf_info["sources_dir"])
    # else: Nothing defined, covered at the end

    added_files = []
    added_dirs = []

    #  TODO: before dealing with all files from all directories, update the list of sources, removing any sub-folders
    #  already included in other folders. Do it with source_list_copy, to avoid strange iterations
    #  This would avoid the issue of sources: [sources_empty/empty_dir_1/, sources_empty/] which adds empty_dir_1
    #  to the root of application_sources/.

    for source in sources_list:
        path_source = Path(source).expanduser()
        if not path_source.exists():
            print(
                f"PROVENANCE | WARNING: A file or directory defined as 'sources' in ro-crate-info.yaml does not exist "
                f"({source})"
            )
            continue
        resolved_source = str(path_source.resolve())
        if os.path.isdir(resolved_source):
            # Adding files twice is not a drama, since add_file_to_crate won't add them twice, but we save traversing directories
            if resolved_source in added_dirs:
                print(
                    f"PROVENANCE | WARNING: A directory addition was attempted twice: {resolved_source}"
                )
                continue  # Do not traverse the directory again
            if any(resolved_source.startswith(dir_item) for dir_item in added_dirs):
                print(
                    f"PROVENANCE | WARNING: A sub-directory addition was attempted twice: {resolved_source}"
                )
                continue
            if any(dir_item.startswith(resolved_source) for dir_item in added_dirs):
                print(
                    f"PROVENANCE | WARNING: A parent directory of a previously added sub-directory is being added. Some "
                    f"files will be traversed twice in: {resolved_source}"
                )
                # Can't continue, we need to traverse the parent directory. Luckily, files won't be added twice
            added_dirs.append(resolved_source)
            for root, dirs, files in os.walk(
                resolved_source, topdown=True, followlinks=True
            ):
                if "__pycache__" in root:
                    continue  # We skip __pycache__ subdirectories
                for f_name in files:
                    if f_name.startswith("*"):
                        # Avoid dealing with symlinks with wildcards
                        continue
                    resolved_file = os.path.join(root, f_name)
                    if resolved_file not in added_files:
                        add_file_to_crate(
                            compss_crate,
                            resolved_file,
                            compss_ver,
                            main_entity,
                            out_profile,
                            resolved_source,
                        )
                        added_files.append(resolved_file)
                    else:
                        print(
                            f"PROVENANCE | WARNING: A file addition was attempted twice: "
                            f"{resolved_file} in {resolved_source}"
                        )
                for dir_name in dirs:
                    # Check if it's an empty directory, needs to be added by hand
                    full_dir_name = os.path.join(root, dir_name)
                    if not os.listdir(full_dir_name):
                        # print(f"PROVENANCE DEBUG | Adding an empty directory. root ({root}), full_dir_name ({full_dir_name}), resolved_source ({resolved_source})")
                        # Workaround to add empty directories in a git repository
                        git_keep = Path(full_dir_name + "/" + ".gitkeep")
                        Path.touch(git_keep)
                        add_file_to_crate(
                            compss_crate,
                            str(git_keep),
                            compss_ver,
                            main_entity,
                            out_profile,
                            resolved_source,
                        )
            if not os.listdir(resolved_source):
                # The root directory itself is empty
                # print(f"PROVENANCE DEBUG | Adding an empty directory. resolved_source ({resolved_source})")
                # Workaround to add empty directories in a git repository
                git_keep = Path(resolved_source + "/" + ".gitkeep")
                Path.touch(git_keep)
                add_file_to_crate(
                    compss_crate,
                    str(git_keep),
                    compss_ver,
                    main_entity,
                    out_profile,
                    resolved_source,
                )
        elif os.path.isfile(resolved_source):
            if resolved_source not in added_files:
                add_file_to_crate(
                    compss_crate,
                    resolved_source,
                    compss_ver,
                    main_entity,
                    out_profile,
                    "",
                )
                added_files.append(resolved_source)
            else:
                print(
                    f"PROVENANCE | WARNING: A file addition was attempted twice: "
                    f"{resolved_source} in {added_dirs}"
                )
        else:
            print(
                f"PROVENANCE | WARNING: A defined source is neither a directory, nor a file ({resolved_source})"
            )

    if len(sources_list) == 0:
        # No sources defined by the user, add the selected main_entity at least
        add_file_to_crate(
            compss_crate, main_entity, compss_ver, main_entity, out_profile, ""
        )
        added_files.append(main_entity)

    # Add auxiliary files as hasPart to the ComputationalWorkflow main file
    # Not working well when an application has several versions (ex: Java matmul files, objects, arrays)
    # for e in compss_crate.data_entities:
    #     if 'ComputationalWorkflow' in e.type:
    #         for file in crate_paths:
    #             if file is not "":
    #                 e.append_to("hasPart", {"@id": file})

    print(f"PROVENANCE | Application source files detected ({len(added_files)})")
    # print(f"PROVENANCE DEBUG | Source files detected: {added_files}")

    print(
        f"PROVENANCE | RO-Crate adding source files TIME: {time.time() - part_time} s"
    )


def add_dataset_file_to_crate(
    compss_crate: ROCrate, in_url: str, persist: bool, common_paths: list
) -> str:
    """
    Add the file (or a reference to it) belonging to the dataset of the application (both input or output)
    When adding local files that we don't want to be physically in the Crate, they must be added with a file:// URI
    CAUTION: If the file has been already added (e.g. for INOUT files) add_file won't succeed in adding a second entity
    with the same name

    :param compss_crate: The COMPSs RO-Crate being generated
    :param in_url: File added as input or output
    :param persist: True to attach the file to the crate, False otherwise
    :param common_paths: List of identified common paths among all dataset files, all finish with '/'

    :returns: The original url if persist is false, the crate_path if persist is true
    """

    # method_time = time.time()

    url_parts = urlsplit(in_url)
    # If in_url ends up with '/', os.path.basename will be empty, thus we need Pathlib
    url_path = Path(url_parts.path)
    final_item_name = url_path.name

    file_properties = {
        "name": final_item_name,
        "sdDatePublished": iso_now(),
        "dateModified": dt.datetime.utcfromtimestamp(os.path.getmtime(url_parts.path))
        .replace(microsecond=0)
        .isoformat(),  # Schema.org
    }  # Register when the Data Entity was last accessible

    if url_parts.scheme == "file":  # Dealing with a local file
        file_properties["contentSize"] = os.path.getsize(url_parts.path)
        crate_path = ""
        # add_file_time = time.time()
        if persist:  # Remove scheme so it is added as a regular file
            for i, item in enumerate(common_paths):  # All files must have a match
                if url_parts.path.startswith(item):
                    cwd_endslash = os.getcwd() + '/'  # os.getcwd does not add the final slash
                    if cwd_endslash == item:
                        # Check if it is the working directory. When this script runs, user application has finished,
                        # so we can ensure cwd is the original folder where the application was started
                        # Workingdir dataset folder, add it to the root
                        crate_path = "dataset/" + url_parts.path[len(item) :]
                        # Slice out the common part of the path
                    else:  # Now includes len(common_paths) == 1
                        cp_path = Path(
                            item
                        )  # Looking for the name of the previous folder
                        crate_path = (
                            "dataset/"
                            # + "folder_"
                            # + str(i)
                            + cp_path.parts[
                                -1
                            ]  # Base name of the identified common path. Now it does not avoid collisions if the user defines the same folder name in two different locations
                            + '/'  # Common part now always ends with '/'
                            + url_parts.path[len(item) :]
                        )  # Slice out the common part of the path
                    break
            # print(f"PROVENANCE DEBUG | Adding {url_parts.path} as {crate_path}")
            compss_crate.add_file(
                source=url_parts.path, dest_path=crate_path, properties=file_properties
            )
            return crate_path
        # else:
        compss_crate.add_file(
            in_url,
            fetch_remote=False,
            validate_url=False,  # True fails at MN4 when file URI points to a node hostname (only localhost works)
            properties=file_properties,
        )
        return in_url
        # add_file_time = time.time() - add_file_time

    if url_parts.scheme == "dir":  # DIRECTORY parameter
        # if persist:
        #     # Add whole dataset, and return. Clean path name first
        #     crate_path = "dataset/" + final_item_name
        #     print(f"PROVENANCE DEBUG | Adding DATASET {url_parts.path} as {crate_path}")
        #     compss_crate.add_tree(source=url_parts.path, dest_path=crate_path, properties=file_properties)
        #     # fetch_remote and validate_url false by default. add_dataset also ensures the URL ends with '/'
        #     return crate_path

        # For directories, describe all files inside the directory
        has_part_list = []
        for root, dirs, files in os.walk(
            url_parts.path, topdown=True, followlinks=True
        ):  # Ignore references to sub-directories (they are not a specific in or out of the workflow),
            # but not their files
            if "__pycache__" in root:
                continue  # We skip __pycache__ subdirectories
            dirs.sort()
            files.sort()
            for f_name in files:
                if f_name.startswith("*"):
                    # Avoid dealing with symlinks with wildcards
                    continue
                listed_file = os.path.join(root, f_name)
                # print(f"PROVENANCE DEBUG: listed_file is {listed_file}")
                dir_f_properties = {
                    "name": f_name,
                    "sdDatePublished": iso_now(),  # Register when the Data Entity was last accessible
                    "dateModified": dt.datetime.utcfromtimestamp(
                        os.path.getmtime(listed_file)
                    )
                    .replace(microsecond=0)
                    .isoformat(),
                    # Schema.org
                    "contentSize": os.path.getsize(listed_file),
                }
                if persist:
                    # url_parts.path includes a final '/'
                    filtered_url = listed_file[len(url_parts.path) :]  # Does not include an initial '/'
                    dir_f_url = "dataset/" + final_item_name + '/'+ filtered_url
                    # print(f"PROVENANCE DEBUG | Adding DATASET FILE {listed_file} as {dir_f_url}")
                    compss_crate.add_file(
                        source=listed_file,
                        dest_path=dir_f_url,
                        fetch_remote=False,
                        validate_url=False,
                        # True fails at MN4 when file URI points to a node hostname (only localhost works)
                        properties=dir_f_properties,
                    )
                else:
                    dir_f_url = "file://" + url_parts.netloc + listed_file
                    compss_crate.add_file(
                        dir_f_url,
                        fetch_remote=False,
                        validate_url=False,
                        # True fails at MN4 when file URI points to a node hostname (only localhost works)
                        properties=dir_f_properties,
                    )
                has_part_list.append({"@id": dir_f_url})

            for dir_name in dirs:
                # Check if it's an empty directory, needs to be added by hand
                full_dir_name = os.path.join(root, dir_name)
                if not os.listdir(full_dir_name):
                    # print(f"PROVENANCE DEBUG | Adding an empty directory in data persistence. root ({root}), full_dir_name ({full_dir_name})")
                    dir_properties = {
                        "sdDatePublished": iso_now(),
                        "dateModified": dt.datetime.utcfromtimestamp(
                            os.path.getmtime(full_dir_name)
                        )
                        .replace(microsecond=0)
                        .isoformat(),  # Schema.org
                    }  # Register when the Data Entity was last accessible
                    if persist:
                        # Workaround to add empty directories in a git repository
                        git_keep = Path(full_dir_name + "/" + ".gitkeep")
                        Path.touch(git_keep)
                        dir_properties["name"] = ".gitkeep"
                        path_final_part = full_dir_name[len(url_parts.path) :]
                        dir_f_url = (
                            "dataset/"
                            + final_item_name
                            + "/"
                            + path_final_part
                            + "/"
                            + ".gitkeep"
                        )
                        # compss_crate.add_dataset(
                        #     source=full_dir_name,
                        #     dest_path=dir_f_url,
                        #     properties=dir_properties,
                        # )
                        # print(f"ADDING DATASET FILE {git_keep} as {dir_f_url}")
                        compss_crate.add_file(
                            source=git_keep,
                            dest_path=dir_f_url,
                            fetch_remote=False,
                            validate_url=False,
                            # True fails at MN4 when file URI points to a node hostname (only localhost works)
                            properties=dir_properties,
                        )
                    else:
                        dir_properties["name"] = dir_name
                        dir_f_url = "file://" + url_parts.netloc + full_dir_name + "/"
                        # Directories must finish with slash
                        compss_crate.add_dataset(
                            source=dir_f_url, properties=dir_properties
                        )
                        has_part_list.append({"@id": dir_f_url})

        # After checking all directory structure, represent correctly the dataset
        if not os.listdir(url_parts.path):
            # The root directory itself is empty
            # print(f"PROVENANCE DEBUG | Adding an empty directory. url_parts.path ({url_parts.path})")
            if persist:
                # Workaround to add empty directories in a git repository
                git_keep = Path(url_parts.path + "/" + ".gitkeep")
                Path.touch(git_keep)
                dir_properties = {
                    "name": ".gitkeep",
                    "sdDatePublished": iso_now(),
                    "dateModified": dt.datetime.utcfromtimestamp(
                        os.path.getmtime(url_parts.path)
                    )
                    .replace(microsecond=0)
                    .isoformat(),  # Schema.org
                }  # Register when the Data Entity was last accessible
                path_in_crate = (
                    "dataset/" + final_item_name + "/" + ".gitkeep"
                )  # Remove resolved_source from full_dir_name, adding basename
                # compss_crate.add_dataset(
                #     source=full_dir_name,
                #     dest_path=path_in_crate,
                #     properties=dir_properties,
                # )
                # print(f"ADDING FILE {git_keep} as {path_in_crate}")
                compss_crate.add_file(
                    source=git_keep,
                    dest_path=path_in_crate,
                    fetch_remote=False,
                    validate_url=False,
                    # True fails at MN4 when file URI points to a node hostname (only localhost works)
                    properties=dir_properties,
                )
                has_part_list.append({"@id": path_in_crate})
                path_in_crate = ("dataset/" + final_item_name + "/")
                # fetch_remote and validate_url false by default. add_dataset also ensures the URL ends with '/'
                dir_properties["name"] = final_item_name
                dir_properties["hasPart"] = has_part_list
                # print(f"ADDING DATASET FOR THE EMPTY DIRECTORY {final_item_name} as {path_in_crate}, with hasPart {has_part_list}")
                compss_crate.add_dataset(
                    source=url_parts.path,
                    dest_path=path_in_crate,
                    properties=dir_properties,
                )
                return path_in_crate
            else:
                # Directories must finish with slash
                compss_crate.add_dataset(source=fix_dir_url(in_url), properties=file_properties)
        else:
            # Directory had content
            file_properties["hasPart"] = has_part_list
            if persist:
                dataset_path = url_parts.path
                path_in_crate = "dataset/" + final_item_name + "/"
                # print(f"PROVENANCE DEBUG | Adding DATASET {dataset_path} as {path_in_crate}")
                compss_crate.add_dataset(
                    source=dataset_path,
                    dest_path=path_in_crate,
                    properties=file_properties,
                )  # fetch_remote and validate_url false by default. add_dataset also ensures the URL ends with '/'
                return path_in_crate
            # else:
            # fetch_remote and validate_url false by default. add_dataset also ensures the URL ends with '/'
            compss_crate.add_dataset(fix_dir_url(in_url), properties=file_properties)

    else:  # Remote file, currently not supported in COMPSs. validate_url already adds contentSize and encodingFormat
        # from the remote file
        compss_crate.add_file(in_url, validate_url=True, properties=file_properties)

    # print(f"Method vs add_file TIME: {time.time() - method_time} vs {add_file_time}")

    return fix_dir_url(in_url)


def wrroc_create_action(
    compss_crate: ROCrate,
    main_entity: str,
    author_list: list,
    ins: list,
    outs: list,
    yaml_content: dict,
) -> str:
    """
    Add a CreateAction term to the ROCrate to make it compliant with WRROC.  RO-Crate WorkflowRun Level 2 profile,
    aka. Workflow Run Crate.

    :param compss_crate: The COMPSs RO-Crate being generated
    :param main_entity: The name of the source file that contains the COMPSs application main() method
    :param author_list: List of authors as described in the YAML
    :param ins: List of input files of the workflow
    :param outs: List of output files of the workflow
    :param yaml_content: Content of the YAML file specified by the user

    :returns: UUID generated for this run
    """

    # Compliance with RO-Crate WorkflowRun Level 2 profile, aka. Workflow Run Crate
    # marenostrum4, nord3, ... BSC_MACHINE would also work
    host_name = os.getenv("SLURM_CLUSTER_NAME")
    if host_name is None:
        host_name = os.getenv("BSC_MACHINE")
        if host_name is None:
            host_name = socket.gethostname()
    job_id = os.getenv("SLURM_JOB_ID")

    main_entity_pathobj = Path(main_entity)

    run_uuid = str(uuid.uuid4())

    if job_id is None:
        name_property = (
            "COMPSs " + main_entity_pathobj.name + " execution at " + host_name
        )
        userportal_url = None
        create_action_id = "#COMPSs_Workflow_Run_Crate_" + host_name + "_" + run_uuid
    else:
        name_property = (
            "COMPSs "
            + main_entity_pathobj.name
            + " execution at "
            + host_name
            + " with JOB_ID "
            + job_id
        )
        userportal_url = "https://userportal.bsc.es/"  # job_id cannot be added, does not match the one in userportal
        create_action_id = (
            "#COMPSs_Workflow_Run_Crate_" + host_name + "_SLURM_JOB_ID_" + job_id
        )
    compss_crate.root_dataset["mentions"] = {"@id": create_action_id}

    # OSTYPE, HOSTTYPE, HOSTNAME defined by bash and not inherited. Changed to "uname -a"
    uname = subprocess.run(["uname", "-a"], stdout=subprocess.PIPE, check=True)
    uname_out = uname.stdout.decode("utf-8")[:-1]  # Remove final '\n'

    # SLURM interesting variables: SLURM_JOB_NAME, SLURM_JOB_QOS, SLURM_JOB_USER, SLURM_SUBMIT_DIR, SLURM_NNODES or
    # SLURM_JOB_NUM_NODES, SLURM_JOB_CPUS_PER_NODE, SLURM_MEM_PER_CPU, SLURM_JOB_NODELIST or SLURM_NODELIST.
    slurm_env_vars = ""
    for name, value in os.environ.items():
        if (
            name.startswith(("SLURM_JOB", "SLURM_MEM", "SLURM_SUBMIT", "COMPSS"))
            and name != "SLURM_JOBID"
        ):
            slurm_env_vars += f"{name}={value} "

    if len(slurm_env_vars) > 0:
        description_property = (
            uname_out + " " + slurm_env_vars[:-1]
        )  # Remove blank space
    else:
        description_property = uname_out

    resolved_main_entity = main_entity
    for entity in compss_crate.get_entities():
        if "ComputationalWorkflow" in entity.type:
            resolved_main_entity = entity.id

    # Register user submitting the workflow
    if "Submitter" in yaml_content:
        compss_crate.add(
            Person(
                compss_crate,
                yaml_content["Submitter"]["orcid"],
                {
                    "name": yaml_content["Submitter"]["name"],
                    "contactPoint": {
                        "@id": "mailto:" + yaml_content["Submitter"]["e-mail"]
                    },
                    "affiliation": {"@id": yaml_content["Submitter"]["ror"]},
                },
            )
        )
        compss_crate.add(
            ContextEntity(
                compss_crate,
                "mailto:" + yaml_content["Submitter"]["e-mail"],
                {
                    "@type": "ContactPoint",
                    "contactType": "Author",
                    "email": yaml_content["Submitter"]["e-mail"],
                    "identifier": yaml_content["Submitter"]["e-mail"],
                    "url": yaml_content["Submitter"]["orcid"],
                },
            )
        )
        compss_crate.add(
            ContextEntity(
                compss_crate,
                yaml_content["Submitter"]["ror"],
                {
                    "@type": "Organization",
                    "name": yaml_content["Submitter"]["organisation_name"],
                },
            )
        )
        submitter = {"@id": yaml_content["Submitter"]["orcid"]}
    else:  # Choose first author, to avoid leaving it empty. May be true most of the times
        if author_list:
            submitter = author_list[0]
            print(
                "PROVENANCE | WARNING: 'Submitter' not specified in ro-crate-info.yaml. First author selected by default."
            )
        else:
            submitter = None
            print(
                "PROVENANCE | WARNING: No 'Authors' or 'Submitter' specified in ro-crate-info.yaml"
            )

    create_action_properties = {
        "@type": "CreateAction",
        "instrument": {"@id": resolved_main_entity},  # Resolved path of the main file
        "actionStatus": {"@id": "http://schema.org/CompletedActionStatus"},
        "endTime": iso_now(),  # Get current time
        "name": name_property,
        "description": description_property,
    }
    if submitter:
        create_action_properties["agent"] = submitter

    create_action = compss_crate.add(
        ContextEntity(compss_crate, create_action_id, create_action_properties)
    )  # id can be something fancy for MN4, otherwise, whatever
    create_action.properties()

    # "subjectOf": {"@id": userportal_url}
    if userportal_url is not None:
        create_action.append_to("subjectOf", userportal_url)

    # "object": [{"@id":}],  # List of inputs
    # "result": [{"@id":}]  # List of outputs
    # Right now neither the COMPSs runtime nor this script check if a file URI is inside a dir URI. This means
    # duplicated entries can be found in the metadata (i.e. a file that is part of a directory, can be added
    # independently). However, this does not add duplicated files if data_persistence is True
    # Hint for controlling duplicates: both 'ins' and 'outs' dir URIs come first on each list
    for item in ins:
        create_action.append_to("object", {"@id": fix_dir_url(item)})
    for item in outs:
        create_action.append_to("result", {"@id": fix_dir_url(item)})
    create_action.append_to("result", {"@id": "./"})  # The generated RO-Crate

    return run_uuid


def get_common_paths(url_list: list) -> list:
    """
    Find the common paths in the list of files passed.

    :param url_list: Sorted list of file URLs as generated by COMPSs runtime

    :returns: List of identified common paths among the URLs
    """

    # print(f"PROVENANCE DEBUG | Input to get_common_paths INS and OUTS: {url_list}")
    list_common_paths = []  # Create common_paths list, with counter of occurrences
    if not url_list:  # Empty list
        return list_common_paths

    # The list comes ordered, so all dir:// references will come first
    # We don't need to skip them, we need to add them, since they are common paths already
    i = 0
    file_found = False
    for item in url_list:
        url_parts = urlsplit(item)
        if url_parts.scheme == "dir":
            if url_parts.path not in list_common_paths:
                list_common_paths.append(url_parts.path)
            i += 1
            # print(f"PROVENANCE DEBUG | ADDING DIRECTORY AS COMMON_PATH {url_parts.path}")
            continue
        else:
            file_found = True
            break

    if not file_found:
        # All are directories
        # print(f"PROVENANCE DEBUG | Resulting list of common paths with only directories is: {list_common_paths}")
        return list_common_paths

    # Add first found file
    url_parts = urlsplit(url_list[i])
    # Need to remove schema and hostname from reference, and filename
    common_path = str(Path(url_parts.path).parents[0])
    i += 1

    url_files_list = url_list[i:]  # Slice out directories and the first file
    for item in url_files_list:
        # url_list is a sorted list, important for this algorithm to work
        # if item and common_path have a common path, store that common path in common_path and continue, until the
        # shortest common path different than 0 has been identified
        # https://docs.python.org/3/library/os.path.html  # os.path.commonpath

        url_parts = urlsplit(item)
        # Remove schema and hostname
        tmp = os.path.commonpath([url_parts.path, common_path])  # url_parts.path does not end with '/'
        if tmp != "/":  # String not empty, they have a common path
            # print(f"PROVENANCE DEBUG | Searching. Previous common path is: {common_path}. tmp: {tmp}")
            common_path = tmp
        else:  # if they don't, we are in a new path, so, store the previous in list_common_paths, and assign the new to common_path
            # print(f"PROVENANCE DEBUG | New root to search common_path: {url_parts.path}")
            if common_path not in list_common_paths:
                list_common_paths.append(common_path)
            common_path = str(
                Path(url_parts.path).parents[0]
            )  # Need to remove filename from url_parts.path

    # Add last element's path
    if common_path not in list_common_paths:
        list_common_paths.append(common_path)

    # All paths internally need to finish with a '/'
    for item in list_common_paths:
        if item[-1] != '/':
            list_common_paths.append(item + '/')
            list_common_paths.remove(item)

    # print(f"PROVENANCE DEBUG | Resulting list of common paths is: {list_common_paths}")

    return list_common_paths


def add_manual_datasets(yaml_term: str, compss_wf_info: dict, data_list: list) -> list:
    """
    Adds to a list of dataset entities (files or directories) the ones specified by the user. At the end, removes any
    file:// references that belong to other dir:// references

    :param yaml_term: Term specified in the YAML file (i.e. 'inputs' or 'outputs')
    :param compss_wf_info: YAML dict to extract info form the application, as specified by the user
    :param data_list: Sorted list of file and dir URLs as generated by COMPSs runtime

    :returns: Updated List of identified common paths among the URLs
    """


    # Add entities defined by the user
    # Input files or directories added by hand from the user
    data_entities_list = []
    if isinstance(compss_wf_info[yaml_term], list):
        data_entities_list.extend(compss_wf_info[yaml_term])
    else:
        data_entities_list.append(compss_wf_info[yaml_term])
    for item in data_entities_list:
        path_data_entity = Path(item).expanduser()

        if not path_data_entity.exists():
            print(
                f"PROVENANCE | WARNING: A file or directory defined as '{yaml_term}' in ro-crate-info.yaml does not exist "
                f"({item})"
            )
            continue
        resolved_data_entity = str(path_data_entity.resolve())

        if os.path.isfile(resolved_data_entity):
            new_data_entity = "file://" + socket.gethostname() + resolved_data_entity
        elif os.path.isdir(resolved_data_entity):
            new_data_entity = (
                "dir://" + socket.gethostname() + resolved_data_entity + "/"
            )
        else:
            print(
                f"FATAL ERROR: a reference is neither a file, nor a directory ({resolved_data_entity})"
            )
            raise FileNotFoundError
        if new_data_entity not in data_list:
            # Checking if a file is in a dir would be costly
            data_list.append(new_data_entity)
        else:
            print(
                f"PROVENANCE | WARNING: A file or directory defined as '{yaml_term}' in ro-crate-info.yaml was already part of the dataset "
                f"({item})"
            )
    data_list.sort()  # Sort again, needed for next methods applied to the list

    # POSSIBLE TODO: keep dir and files in separated lists to avoid traversing them too many times, improving efficiency

    # Now erase any file:// that is inside dir://
    i = 0
    directories_list = []
    file_found = False
    for item in data_list:
        url_parts = urlsplit(item)
        if url_parts.scheme == "dir":
            if url_parts.path not in directories_list and not (
                any(
                    url_parts.path.startswith(dir_item) for dir_item in directories_list
                )
            ):
                directories_list.append(url_parts.path)
            i += 1
            continue
        else:
            file_found = True
            break
    if file_found:  # Not all are directories
        #  TODO Can this two loops be merged????
        data_list_copy = (
            data_list.copy()
        )  # So we can remove the element we are iterating upon

        for item in data_list_copy:
            # Check both dir:// and file:// references
            url_parts = urlsplit(item)
            if any(
                (url_parts.path != dir_path and url_parts.path.startswith(dir_path))
                for dir_path in directories_list
            ):
                # If the url dir:// does not finish with a slash, can add errors (e.g. /inputs vs /inputs.zip)
                print(
                    f"PROVENANCE | WARNING: Item {url_parts.path} removed as {yaml_term}, since it already belongs to a dataset"
                )
                data_list.remove(item)

            # if any((url_parts.path != dir_path and url_parts.path.startswith(dir_path)) for dir_path in directories_list):
            #     # if the url dir:// does not finish with a slash, can add errors (e.g. /inputs vs /inputs.zip)
            #     print(
            #         f"PROVENANCE | WARNING: Item {item} removed as {yaml_term}, since it already belongs to a dataset"
            #     )
            #     data_list.remove(item)

    print(
        f"PROVENANCE | Manually added data assets as '{yaml_term}' ({len(data_entities_list)})"
    )

    return data_list


def fix_in_files_at_out_dirs(
    inputs_list: list, outputs_list: list
) -> typing.Tuple[list, list]:
    """
    Remove any file inputs that the user may have declared as directory outputs

    :param inputs_list: list of all input directories and files as URLs
    :param compss_wf_info: list of all output directories and files as URLs


    :returns: Updated inputs and outputs lists
    """

    # Now erase any file:// input that is included as dir:// output
    directories_list = []
    file_found = False
    for item in outputs_list:
        url_parts = urlsplit(item)
        if url_parts.scheme == "dir":
            if url_parts.path not in directories_list:
                directories_list.append(url_parts.path)
        else:
            file_found = True
            break

    i = 0
    file_found = False
    for item in inputs_list:
        url_parts = urlsplit(item)
        if url_parts.scheme == "dir":
            i += 1
        else:
            file_found = True
            break

    if not file_found:
        return inputs_list, outputs_list

    url_files_list = inputs_list[i:]  # Slice out directories
    for item in url_files_list:
        url_parts = urlsplit(item)
        if any(url_parts.path.startswith(dir_path) for dir_path in directories_list):
            print(
                f"PROVENANCE | WARNING: Metadata of an input file has been removed since it is included at an output directory: {url_parts.path}"
            )
            inputs_list.remove(item)

    # print(f"PROVENANCE DEBUG | RESULT FROM fix_in_files_at_out_dirs:\n {inputs_list}")

    return inputs_list, outputs_list


def main():
    """
    Generate an RO-Crate from a COMPSs execution dataprovenance.log file.

    :param None

    :returns: None
    """

    exec_time = time.time()

    yaml_template = (
        "COMPSs Workflow Information:\n"
        "  name: Name of your COMPSs application\n"
        "  description: Detailed description of your COMPSs application\n"
        "  license: Apache-2.0\n"
        "    # URL preferred, but these strings are accepted: https://about.workflowhub.eu/Workflow-RO-Crate/#supported-licenses\n"
        "  sources: [/absolute_path_to/dir_1/, relative_path_to/dir_2/, main_file.py, relative_path/aux_file_1.py, /abs_path/aux_file_2.py]\n"
        "    # List of application source files and directories. Relative or absolute paths can be used.\n"
        "  sources_main_file: my_main_file.py\n"
        "    # Optional: Manually specify the name of the main file of the application, located in one of the 'sources' defined.\n"
        "    # Relative paths from a 'sources' entry, or absolute paths can be used.\n"
        "  data_persistence: False\n"
        "    # True to include all input and output files of the application in the resulting crate.\n"
        "    # If False, input and output files of the application won't be included, just referenced. False by default or if not set.\n"
        "  inputs: [/abs_path_to/dir_1, rel_path_to/dir_2, file_1, rel_path/file_2]\n"
        "    # Optional: Manually specify the inputs of the workflow. Relative or absolute paths can be used.\n"
        "  outputs: [/abs_path_to/dir_1, rel_path_to/dir_2, file_1, rel_path/file_2]\n"
        "    # Optional: Manually specify the outputs of the workflow. Relative or absolute paths can be used.\n"
        "\n"
        "Authors:\n"
        "  - name: Author_1 Name\n"
        "    e-mail: author_1@email.com\n"
        "    orcid: https://orcid.org/XXXX-XXXX-XXXX-XXXX\n"
        "    organisation_name: Institution_1 name\n"
        "    ror: https://ror.org/XXXXXXXXX\n"
        "      # Find them in ror.org\n"
        "  - name: Author_2 Name\n"
        "    e-mail: author2@email.com\n"
        "    orcid: https://orcid.org/YYYY-YYYY-YYYY-YYYY\n"
        "    organisation_name: Institution_2 name\n"
        "    ror: https://ror.org/YYYYYYYYY\n"
        "      # Find them in ror.org\n"
        "\n"
        "Submitter:\n"
        "  name: Name\n"
        "  e-mail: submitter@email.com\n"
        "  orcid: https://orcid.org/XXXX-XXXX-XXXX-XXXX\n"
        "  organisation_name: Submitter Institution name\n"
        "  ror: https://ror.org/XXXXXXXXX\n"
        "    # Find them in ror.org\n"
    )

    compss_crate = ROCrate()

    # First, read values defined by user from ro-crate-info.yaml
    try:
        with open(INFO_YAML, "r", encoding="utf-8") as f_p:
            try:
                yaml_content = yaml.safe_load(f_p)
            except yaml.YAMLError as exc:
                print(exc)
                raise exc
    except IOError:
        with open("ro-crate-info_TEMPLATE.yaml", "w", encoding="utf-8") as f_t:
            f_t.write(yaml_template)
            print(
                "PROVENANCE | ERROR: YAML file ro-crate-info.yaml not found in your working directory. A template"
                " has been generated in file ro-crate-info_TEMPLATE.yaml"
            )
        raise

    # Generate Root entity section in the RO-Crate
    compss_wf_info, author_list = root_entity(compss_crate, yaml_content)

    # Get mainEntity from COMPSs runtime log dataprovenance.log
    compss_ver, main_entity, out_profile = get_main_entities(compss_wf_info)

    # Process set of accessed files, as reported by COMPSs runtime.
    # This must be done before adding the Workflow to the RO-Crate
    ins, outs = process_accessed_files()

    # Add application source files to the RO-Crate, that will also be physically in the crate
    add_application_source_files(
        compss_crate, compss_wf_info, compss_ver, main_entity, out_profile
    )

    # Add in and out files, not to be physically copied in the Crate by default

    # First, add to the lists any inputs or outputs defined by the user, in case they exist
    if "inputs" in compss_wf_info:
        ins = add_manual_datasets("inputs", compss_wf_info, ins)
    if "outputs" in compss_wf_info:
        outs = add_manual_datasets("outputs", compss_wf_info, outs)

    ins, outs = fix_in_files_at_out_dirs(ins, outs)

    # Merge lists to avoid duplication when detecting common_paths
    ins_and_outs = ins.copy() + outs.copy()
    ins_and_outs.sort()  # Put together shared paths between ins an outs

    # print(f"PROVENANCE DEBUG | List of ins and outs: {ins_and_outs}")

    # The list has at this point detected ins and outs, but also added any ins an outs defined by the user

    list_common_paths = []
    part_time = time.time()
    if (
        "data_persistence" in compss_wf_info
        and compss_wf_info["data_persistence"] is True
    ):
        persistence = True
        list_common_paths = get_common_paths(ins_and_outs)
    else:
        persistence = False

    fixed_ins = []  # ins are file://host/path/file, fixed_ins are crate_path/file
    for item in ins:
        fixed_ins.append(
            add_dataset_file_to_crate(
                compss_crate, item, persistence, list_common_paths
            )
        )
    print(
        f"PROVENANCE | RO-Crate adding input files TIME (Persistence: {persistence}): "
        f"{time.time() - part_time} s"
    )

    part_time = time.time()

    fixed_outs = []
    for item in outs:
        fixed_outs.append(
            add_dataset_file_to_crate(
                compss_crate, item, persistence, list_common_paths
            )
        )
    print(
        f"PROVENANCE | RO-Crate adding output files TIME (Persistence: {persistence}): "
        f"{time.time() - part_time} s"
    )

    # print(f"FIXED_INS: {fixed_ins}")
    # print(f"FIXED_OUTS: {fixed_outs}")
    # Register execution details using WRROC profile
    # Compliance with RO-Crate WorkflowRun Level 2 profile, aka. Workflow Run Crate
    run_uuid = wrroc_create_action(
        compss_crate, main_entity, author_list, fixed_ins, fixed_outs, yaml_content
    )

    # ro-crate-py does not deal with profiles
    # compss_crate.metadata.append_to(
    #     "conformsTo", {"@id": "https://w3id.org/workflowhub/workflow-ro-crate/1.0"}
    # )

    #  Code from runcrate https://github.com/ResearchObject/runcrate/blob/411c70da556b60ee2373fea0928c91eb78dd9789/src/runcrate/convert.py#L270
    profiles = []
    for proc in "process", "workflow":
        id_ = f"{PROFILES_BASE}/{proc}/{PROFILES_VERSION}"
        profiles.append(
            compss_crate.add(
                ContextEntity(
                    compss_crate,
                    id_,
                    properties={
                        "@type": "CreativeWork",
                        "name": f"{proc.title()} Run Crate",
                        "version": PROFILES_VERSION,
                    },
                )
            )
        )
    # In the future, this could go out of sync with the wroc
    # profile added by ro-crate-py to the metadata descriptor
    wroc_profile_id = (
        f"https://w3id.org/workflowhub/workflow-ro-crate/{WROC_PROFILE_VERSION}"
    )
    profiles.append(
        compss_crate.add(
            ContextEntity(
                compss_crate,
                wroc_profile_id,
                properties={
                    "@type": "CreativeWork",
                    "name": "Workflow RO-Crate",
                    "version": WROC_PROFILE_VERSION,
                },
            )
        )
    )
    compss_crate.root_dataset["conformsTo"] = profiles

    # Debug
    # for e in compss_crate.get_entities():
    #    print(e.id, e.type)

    # Dump to file
    part_time = time.time()
    folder = "COMPSs_RO-Crate_" + run_uuid + "/"
    compss_crate.write(folder)
    print(f"PROVENANCE | RO-Crate writing to disk TIME: {time.time() - part_time} s")
    print(
        f"PROVENANCE | Workflow Provenance generation TOTAL EXECUTION TIME: {time.time() - exec_time} s"
    )
    print(
        f"PROVENANCE | COMPSs Workflow Provenance successfully generated in sub-folder:\n\t{folder}"
    )


if __name__ == "__main__":

    # Usage: python /path_to/generate_COMPSs_RO-Crate.py ro-crate-info.yaml /path_to/dataprovenance.log
    if len(sys.argv) != 3:
        print(
            "PROVENANCE | Usage: python /path_to/generate_COMPSs_RO-Crate.py "
            "ro-crate-info.yaml /path_to/dataprovenance.log"
        )
        sys.exit()
    else:
        INFO_YAML = sys.argv[1]
        DP_LOG = sys.argv[2]
        path_dplog = Path(sys.argv[2])
        complete_graph = path_dplog.parent / "monitor/complete_graph.svg"
    main()
