#!/usr/bin/env python3
import re

import click
import subprocess
import json
import yaml
import requests
import pathlib
from typing import Tuple, Dict, List


class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def execute(cmd_list) -> Tuple[int, str, str]:
    p = subprocess.Popen(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = p.communicate()
    return p.returncode, stdout, stderr


@click.command()
@click.option('-r', '--release', required=True, help='Release name (e.g. 4.10)')
def run(release):
    release_stream = f'{release}.0-0.ci'
    req = requests.get(f'https://amd64.ocp.releases.ci.openshift.org/api/v1/releasestream/{release_stream}/latest')
    if req.status_code != 200:
        print(f'Unable to find latest for release controller stream: {release_stream}')
        exit(1)

    latest_release = req.json()['name']
    print(f'Analyzing release: {latest_release}')
    latest_pullspec = f'registry.ci.openshift.org/ocp/release:{latest_release}'
    rc, stdout, stderr = execute(['oc', 'adm', 'release', 'info', '--output=json', latest_pullspec])

    if rc != 0:
        print(f'Unable to retrieve release info for {latest_pullspec}')
        exit(1)

    ci_operartor_config_dir = pathlib.Path(__file__).parent.parent.joinpath('ci-operator', 'config')
    ri = json.loads(stdout)
    unsatisfied, satisfied = (0, 0)
    for tag in ri['references']['spec']['tags']:
        component_name = tag["name"]
        print(f'  Checking component: {component_name}')
        if component_name == 'machine-os-content':
            print(f'    Skipping...')
            continue

        source_location = tag['annotations']['io.openshift.build.source-location']
        _, org, repo = source_location.rsplit('/', 2)
        repo_config_dir = ci_operartor_config_dir.joinpath(org, repo)
        ci_op_config_files = list(repo_config_dir.glob("*-master.y*ml")) + list(repo_config_dir.glob("*-main.y*ml")) + list(repo_config_dir.glob(f"*-openshift-{release}.y*ml"))
        if not ci_op_config_files:
            print('    Unable for find ci-operator config file for default branch!')
            exit(1)

        ci_op_config = yaml.safe_load(ci_op_config_files[0].read_text())
        tests = ci_op_config['tests']

        checklist = {
            re.compile(r'e2e-[^-]+$'): False,
            re.compile(r'e2e-[^-]*[-]?upgrade$'): False,
            re.compile(r'e2e-[^-]+-serial$'): False,
        }

        tests_run = []
        for test in tests:
            test_name = test['as']
            tests_run.append(test_name)
            for regex in checklist.keys():
                if regex.match(test_name):
                    checklist[regex] = True

        print(f'    Configured tests: {tests_run}')
        bad_config = False
        for regex, found in checklist.items():
            if not found:
                bad_config = True
                print(f'{bcolors.WARNING}    Unable to find: {regex.pattern}{bcolors.ENDC}')
                unsatisfied += 1

        if not bad_config:
            print(f'{bcolors.OKGREEN}    All required tests are being run{bcolors.ENDC}')
            satisfied += 1

        print()
    print()
    print('Summary: ')
    print(f'  Compliant: {satisfied}')
    print(f'  Noncompliant: {unsatisfied}')

if __name__ == '__main__':
    run()
