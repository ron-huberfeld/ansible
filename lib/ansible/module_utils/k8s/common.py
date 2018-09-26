# Copyright 2018 Red Hat | Ansible
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import absolute_import, division, print_function

import copy
import json
import os


from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.six import iteritems, string_types

try:
    import kubernetes
    import openshift
    from openshift.dynamic import DynamicClient
    from openshift.dynamic.exceptions import ResourceNotFoundError, ResourceNotUniqueError
    HAS_K8S_MODULE_HELPER = True
except ImportError:
    HAS_K8S_MODULE_HELPER = False

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

try:
    import dictdiffer
    HAS_DICTDIFFER = True
except ImportError:
    HAS_DICTDIFFER = False

try:
    import urllib3
    urllib3.disable_warnings()
except ImportError:
    pass


def list_dict_str(value):
    if isinstance(value, list):
        return value
    elif isinstance(value, dict):
        return value
    elif isinstance(value, string_types):
        return value
    raise TypeError


ARG_ATTRIBUTES_BLACKLIST = ('property_path',)

COMMON_ARG_SPEC = {
    'state': {
        'default': 'present',
        'choices': ['present', 'absent'],
    },
    'force': {
        'type': 'bool',
        'default': False,
    },
    'resource_definition': {
        'type': list_dict_str,
        'aliases': ['definition', 'inline']
    },
    'src': {
        'type': 'path',
    },
    'kind': {},
    'name': {},
    'namespace': {},
    'api_version': {
        'default': 'v1',
        'aliases': ['api', 'version'],
    },
}

AUTH_ARG_SPEC = {
    'kubeconfig': {
        'type': 'path',
    },
    'context': {},
    'host': {},
    'api_key': {
        'no_log': True,
    },
    'username': {},
    'password': {
        'no_log': True,
    },
    'verify_ssl': {
        'type': 'bool',
    },
    'ssl_ca_cert': {
        'type': 'path',
    },
    'cert_file': {
        'type': 'path',
    },
    'key_file': {
        'type': 'path',
    },
}


class K8sAnsibleMixin(object):
    _argspec_cache = None

    @property
    def argspec(self):
        """
        Introspect the model properties, and return an Ansible module arg_spec dict.
        :return: dict
        """
        if self._argspec_cache:
            return self._argspec_cache
        argument_spec = copy.deepcopy(COMMON_ARG_SPEC)
        argument_spec.update(copy.deepcopy(AUTH_ARG_SPEC))
        self._argspec_cache = argument_spec
        return self._argspec_cache

    def get_api_client(self, **auth_params):
        auth_args = AUTH_ARG_SPEC.keys()

        auth_params = auth_params or getattr(self, 'params', {})
        auth = copy.deepcopy(auth_params)

        # If authorization variables aren't defined, look for them in environment variables
        for key, value in iteritems(auth_params):
            if key in auth_args and value is None:
                env_value = os.getenv('K8S_AUTH_{0}'.format(key.upper()), None)
                if env_value is not None:
                    auth[key] = env_value

        def auth_set(*names):
            return all([auth.get(name) for name in names])

        if auth_set('username', 'password', 'host') or auth_set('api_key', 'host'):
            # We have enough in the parameters to authenticate, no need to load incluster or kubeconfig
            pass
        elif auth_set('kubeconfig', 'context'):
            kubernetes.config.load_kube_config(auth.get('kubeconfig'), auth.get('context'))
        else:
            # First try to do incluster config, then kubeconfig
            try:
                kubernetes.config.load_incluster_config()
            except kubernetes.config.ConfigException:
                kubernetes.config.load_kube_config(auth.get('kubeconfig'), auth.get('context'))

        # Override any values in the default configuration with Ansible parameters
        configuration = kubernetes.client.Configuration()
        for key, value in iteritems(auth):
            if key in auth_args and value is not None:
                if key == 'api_key':
                    setattr(configuration, key, {'authorization': "Bearer {0}".format(value)})
                else:
                    setattr(configuration, key, value)

        kubernetes.client.Configuration.set_default(configuration)
        return DynamicClient(kubernetes.client.ApiClient(configuration))

    def find_resource(self, kind, api_version, fail=False):
        for attribute in ['kind', 'name', 'singular_name']:
            try:
                return self.client.resources.get(**{'api_version': api_version, attribute: kind})
            except (ResourceNotFoundError, ResourceNotUniqueError):
                pass
        try:
            return self.client.resources.get(api_version=api_version, short_names=[kind])
        except (ResourceNotFoundError, ResourceNotUniqueError):
            if fail:
                self.fail(msg='Failed to find exact match for {0}.{1} by [kind, name, singularName, shortNames]'.format(api_version, kind))

    def kubernetes_facts(self, kind, api_version, name=None, namespace=None, label_selectors=None, field_selectors=None):
        resource = self.find_resource(kind, api_version)
        try:
            result = resource.get(name=name,
                                  namespace=namespace,
                                  label_selector=','.join(label_selectors),
                                  field_selector=','.join(field_selectors)).to_dict()
        except openshift.dynamic.exceptions.NotFoundError:
            return dict(items=[])

        if 'items' in result:
            return dict(resources=result['items'])
        else:
            return dict(resources=[result])

    def remove_aliases(self):
        """
        The helper doesn't know what to do with aliased keys
        """
        for k, v in iteritems(self.argspec):
            if 'aliases' in v:
                for alias in v['aliases']:
                    if alias in self.params:
                        self.params.pop(alias)

    def load_resource_definitions(self, src):
        """ Load the requested src path """
        result = None
        path = os.path.normpath(src)
        if not os.path.exists(path):
            self.fail(msg="Error accessing {0}. Does the file exist?".format(path))
        try:
            with open(path, 'r') as f:
                result = list(yaml.safe_load_all(f))
        except (IOError, yaml.YAMLError) as exc:
            self.fail(msg="Error loading resource_definition: {0}".format(exc))
        return result

    @staticmethod
    def diff_objects(existing, new):
        if not HAS_DICTDIFFER:
            return False, []

        diffs = list(dictdiffer.diff(new, existing))
        match = len(diffs) == 0
        return match, diffs


class KubernetesAnsibleModule(AnsibleModule, K8sAnsibleMixin):
    resource_definition = None
    api_version = None
    kind = None

    def __init__(self, *args, **kwargs):

        kwargs['argument_spec'] = self.argspec
        AnsibleModule.__init__(self, *args, **kwargs)

        if not HAS_K8S_MODULE_HELPER:
            self.fail_json(msg="This module requires the OpenShift Python client. Try `pip install openshift`")
        self.openshift_version = openshift.__version__

        if not HAS_YAML:
            self.fail_json(msg="This module requires PyYAML. Try `pip install PyYAML`")

    def execute_module(self):
        raise NotImplementedError()

    def fail(self, msg=None):
        self.fail_json(msg=msg)
