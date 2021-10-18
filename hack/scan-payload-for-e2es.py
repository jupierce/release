#!/usr/bin/env python3
import re

import click
import subprocess
import json
import yaml
import requests
import pathlib
from typing import Tuple, Dict, List
from urllib.parse import quote


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
    ci_release_stream = f'{release}.0-0.ci'
    req = requests.get(f'https://amd64.ocp.releases.ci.openshift.org/api/v1/releasestream/{ci_release_stream}/latest')
    if req.status_code != 200:
        print(f'Unable to find latest for release controller stream: {ci_release_stream}')
        exit(1)

    latest_release = req.json()['name']
    print(f'Analyzing CI release: {latest_release}')
    latest_pullspec = f'registry.ci.openshift.org/ocp/release:{latest_release}'
    rc, stdout, stderr = execute(['oc', 'adm', 'release', 'info', '--output=json', latest_pullspec])

    if rc != 0:
        print(f'Unable to retrieve release info for {latest_pullspec}')
        exit(1)

    ci_ri = json.loads(stdout)

    art_release_stream = f'{release}.0-0.nightly'
    req = requests.get(f'https://amd64.ocp.releases.ci.openshift.org/api/v1/releasestream/{art_release_stream}/latest')
    if req.status_code != 200:
        print(f'Unable to find latest for release controller stream: {art_release_stream}')
        exit(1)

    latest_release = req.json()['name']
    print(f'Analyzing nightly release: {latest_release}')
    latest_pullspec = f'registry.ci.openshift.org/ocp/release:{latest_release}'
    rc, stdout, stderr = execute(['oc', 'adm', 'release', 'info', '--output=json', latest_pullspec])

    if rc != 0:
        print(f'Unable to retrieve release info for {latest_pullspec}')
        exit(1)

    art_ri = json.loads(stdout)
    art_tags: Dict[str, Dict] = {}  # maps component_name to ART tag entry

    for tag in art_ri['references']['spec']['tags']:
        component_name = tag["name"]
        art_tags[component_name] = tag

    ci_operartor_config_dir = pathlib.Path(__file__).parent.parent.joinpath('ci-operator', 'config')
    unsatisfied, satisfied, broken = (0, 0, 0)
    for tag in ci_ri['references']['spec']['tags']:
        component_name = tag["name"]

        if component_name in art_tags:
            # ART will usually have component information available
            tag = art_tags[component_name]

        pullspec = tag['from']['name']
        if component_name == 'machine-os-content':
            continue

        source_location = tag['annotations']['io.openshift.build.source-location']
        _, org, repo = source_location.rsplit('/', 2)
        repo_config_dir = ci_operartor_config_dir.joinpath(org, repo)
        ci_op_config_files = list(repo_config_dir.glob("*-master.y*ml")) + list(repo_config_dir.glob("*-main.y*ml")) + list(repo_config_dir.glob(f"*-openshift-{release}.y*ml"))
        if not ci_op_config_files:
            print('    Unable for find ci-operator config file for default branch!')
            exit(1)

        ci_op_config_file = ci_op_config_files[0]
        ci_op_config = yaml.safe_load(ci_op_config_file.read_text())
        tests = ci_op_config['tests']

        any_e2e = re.compile(r'openshift-e2e-.+$')
        ok_e2e = False
        e2e_upgrade = re.compile(r'openshift-e2e-[^-]*[-]?upgrade$')
        ok_upgrade = False
        e2e_serial = re.compile(r'openshift-e2e-[^-]+-serial$')
        ok_serial = False

        tests_run = []
        for test in tests:
            tests_run.append(test['as'])
            if 'steps' not in test or 'workflow' not in test['steps']:
                continue
            test_name = test['steps']['workflow']

            if e2e_serial.match(test_name):
                ok_serial = True
            elif e2e_upgrade.match(test_name):
                ok_upgrade = True
            elif any_e2e.match(test_name) or test_name == 'baremetalds-e2e':
                ok_e2e = True

        if not ok_serial and not ok_e2e and not ok_upgrade:
            print(f'  Checking component: {component_name} ({str(ci_op_config_file)}')
            print(f'    Configured tests: {tests_run}')
            print(f'    ALL recommended tests are missing.')
            rc, stdout, stderr = execute(['oc', 'image', 'info', '--output=json', pullspec])
            if rc != 0:
                print(f'Unable to retrieve image info for: {pullspec}')
                exit(1)

            image_info = json.loads(stdout)
            labels = image_info['config']['config']['Labels']
            product = labels.get('io.openshift.maintainer.product', 'OpenShift Container Platform')
            component = labels.get('io.openshift.maintainer.component', None)
            if component == 'Release':
                component = 'Unknown'
            subcomponent = labels.get('io.openshift.maintainer.subcomponent', None)

            # if '-main.yaml' in str(ci_op_config_file):
            #     branch = 'main'
            # else:
            #     branch = 'master'
            # print(f'    Owners: https://github.com/{org}/{repo}/blob/{branch}/OWNERS')
            classification = 'Red Hat'
            short_desc = f'No e2e CI presubmit configured for release component {component_name}'
            comment = f'''Components in the OCP release payload must have at least one e2e test workflow configured in their presubmit CI tests. This helps prevent significant regressions in CI when changes are made. 
            
An automated scan has detected {component_name} to be noncompliant with this policy. To rectify this, please introduce an e2e presubmit test for this component in https://github.com/openshift/release. An e2e-* and e2e-*-serial presubmit is recommended for each 4.x release which includes your component. 

Example component with e2e tests enabled: https://github.com/openshift/release/blob/a941d278fbc33f6ebe2a75256c77f344b572a067/ci-operator/config/openshift/cluster-api-provider-aws/openshift-cluster-api-provider-aws-master.yaml#L61 .                 
'''
            params = {
                'classification': classification,
                'product': product,
                'short_desc': short_desc,
                'comment': comment,
                'version': release,
            }
            if component:
                params['component'] = component

            encoded_params = ''
            for k, v in params.items():
                encoded_params += f'{k}={quote(v)}&'

            create_bug = f'https://bugzilla.redhat.com/enter_bug.cgi?{encoded_params}'
            print(f'    Create bug: {create_bug}')
            if subcomponent:
                print(f'    File under subcomponent: {subcomponent}')
            print()
            broken += 1
        elif not ok_serial or not ok_e2e or not ok_upgrade:
            unsatisfied += 1
        else:
            #    print(f'{bcolors.OKGREEN}    All required tests are being run{bcolors.ENDC}')
            satisfied += 1

    print()
    print('Summary: ')
    print(f'  Compliant: {satisfied}')
    print(f'  Noncompliant: {unsatisfied}')
    print(f'  Broken: {broken}')


if __name__ == '__main__':
    run()
