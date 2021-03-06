import os
import pytest
import tempfile
from base64 import b64encode
from pathlib import Path
from unittest import mock
from subprocess import CalledProcessError

from charms.layer import openstack


def patch_fixture(patch_target):
    @pytest.fixture()
    def _fixture():
        with mock.patch(patch_target) as m:
            yield m
    return _fixture


urlopen = patch_fixture('charms.layer.openstack.urlopen')
log_err = patch_fixture('charms.layer.openstack.log_err')
_load_creds = patch_fixture('charms.layer.openstack._load_creds')
detect_octavia = patch_fixture('charms.layer.openstack.detect_octavia')
run = patch_fixture('subprocess.run')
_run_with_creds = patch_fixture('charms.layer.openstack._run_with_creds')
_openstack = patch_fixture('charms.layer.openstack._openstack')
_neutron = patch_fixture('charms.layer.openstack._neutron')
LoadBalancerClient = patch_fixture('charms.layer.openstack.LoadBalancerClient')
OctaviaLBClient = patch_fixture('charms.layer.openstack.OctaviaLBClient')
NeutronLBClient = patch_fixture('charms.layer.openstack.NeutronLBClient')
_default_subnet = patch_fixture('charms.layer.openstack._default_subnet')
sleep = patch_fixture('time.sleep')


@pytest.fixture
def cert_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        cert_file = Path(tmpdir) / 'test.crt'
        with mock.patch('charms.layer.openstack.CA_CERT_FILE', cert_file):
            yield cert_file


@pytest.fixture
def config():
    with mock.patch('charms.layer.openstack.config', {}) as config:
        yield config


@pytest.fixture
def impl():
    with mock.patch.object(openstack.LoadBalancer, '_get_impl') as _get_impl:
        _get_impl.return_value = mock.Mock(spec=openstack.BaseLBImpl)
        yield _get_impl()


@pytest.fixture
def kv():
    with mock.patch.object(openstack, 'kv') as kv:
        yield kv()


def test_determine_version(urlopen, log_err):
    assert openstack._determine_version({'version': 3}, None) == '3'
    assert not urlopen.called
    assert not log_err.called

    assert openstack._determine_version({}, 'https://endpoint/2') == '2'
    assert openstack._determine_version({}, 'https://endpoint/v2') == '2'
    assert openstack._determine_version({}, 'https://endpoint/v2.0') == '2.0'
    assert not urlopen.called
    assert not log_err.called

    read = urlopen().__enter__().read
    read.return_value = (
        b'{"version": {"id": "v3.12", "status": "stable", '
        b'"updated": "2019-01-22T00:00:00Z", "links": [{"rel": "self", '
        b'"href": "http://10.244.40.88:5000/v3/"}], "media-types": [{'
        b'"base": "application/json", '
        b'"type": "application/vnd.openstack.identity-v3+json"}]}}')
    assert openstack._determine_version({}, 'https://endpoint/') == '3'
    assert not log_err.called

    read.return_value = b'.'
    assert openstack._determine_version({}, 'https://endpoint/') is None
    assert log_err.called

    read.return_value = b'\xff'
    assert openstack._determine_version({}, 'https://endpoint/') is None

    read.return_value = b'{}'
    assert openstack._determine_version({}, 'https://endpoint/') is None


def _b64(s):
    return b64encode(s.encode('utf8')).decode('utf8')


def test_run_with_creds(cert_file, _load_creds, run):
    _load_creds.return_value = {
        'auth_url': 'auth_url',
        'region': 'region',
        'username': 'username',
        'password': 'password',
        'user_domain_name': 'user_domain_name',
        'project_domain_name': 'project_domain_name',
        'project_name': 'project_name',
        'endpoint_tls_ca': _b64('endpoint_tls_ca'),
        'version': '3',
    }
    with mock.patch.dict(os.environ, {'PATH': 'path'}):
        openstack._run_with_creds('my', 'args')
    assert cert_file.exists()
    assert cert_file.read_text() == 'endpoint_tls_ca\n'
    assert run.call_args == mock.call(('my', 'args'), env={
        'PATH': '/snap/bin:path',
        'OS_AUTH_URL': 'auth_url',
        'OS_USERNAME': 'username',
        'OS_PASSWORD': 'password',
        'OS_REGION_NAME': 'region',
        'OS_USER_DOMAIN_NAME': 'user_domain_name',
        'OS_PROJECT_NAME': 'project_name',
        'OS_PROJECT_DOMAIN_NAME': 'project_domain_name',
        'OS_IDENTITY_API_VERSION': '3',
        'OS_CACERT': str(cert_file),
    }, check=True, stdout=mock.ANY)

    _load_creds.return_value['endpoint_tls_ca'] = _b64('foo')
    openstack._run_with_creds('my', 'args')
    assert cert_file.read_text() == 'foo\n'

    _load_creds.return_value['endpoint_tls_ca'] = None
    del _load_creds.return_value['version']
    openstack._run_with_creds('my', 'args')
    env = run.call_args[1]['env']
    assert env['OS_CACERT'] == str(cert_file)
    assert 'OS_IDENTITY_API_VERSION' not in env

    cert_file.unlink()
    _load_creds.return_value['version'] = None
    openstack._run_with_creds('my', 'args')
    env = run.call_args[1]['env']
    assert 'OS_CACERT' not in env
    assert 'OS_IDENTITY_API_VERSION' not in env


def test_default_subnet(_openstack):
    members = [('192.168.0.1', 80), ('10.0.0.1', 80)]
    _openstack.return_value = [
        {'Name': 'a', 'Subnet': '192.168.0.0/24'},
        {'Name': 'b', 'Subnet': '10.0.0.0/16'},
    ]
    assert openstack._default_subnet(members) == 'a'
    assert openstack._default_subnet(list(reversed(members))) == 'b'
    with pytest.raises(openstack.OpenStackLBError):
        openstack._default_subnet([('10.1.0.1', 80)])


def test_get_or_create(kv):
    args = ('app', '80', 'subnet', 'alg', None, False)
    with mock.patch.object(openstack.LoadBalancer, 'create') as create:
        kv.get.return_value = {'sg_id': 'sg_id',
                               'fip': 'fip',
                               'address': 'address',
                               'members': [[1, 2], [3, 4]]}
        lb = openstack.LoadBalancer.get_or_create(*args)
        assert not create.called
        assert lb.name == 'openstack-integrator-1234-app'
        assert lb.port == '80'
        assert lb.subnet == 'subnet'
        assert lb.algorithm == 'alg'
        assert lb.fip_net is None
        assert lb.manage_secgrps is False
        assert lb._key == 'created_lbs.openstack-integrator-1234-app'
        assert lb.sg_id == 'sg_id'
        assert lb.fip == 'fip'
        assert lb.address == 'address'
        assert lb.members == {(1, 2), (3, 4)}
        assert lb.is_created is True

        kv.get.return_value = None
        lb = openstack.LoadBalancer.get_or_create(*args)
        assert create.called
        assert lb.sg_id is None
        assert lb.fip is None
        assert lb.address is None
        assert lb.members == set()

        create.side_effect = CalledProcessError(1, 'cmd')
        with pytest.raises(openstack.OpenStackLBError):
            openstack.LoadBalancer.get_or_create(*args)


def test_create_new(impl, sleep, log_err):
    openstack.kv().get.return_value = None
    lb = openstack.LoadBalancer('app', '80', 'subnet', 'alg', None, False)
    assert not lb.is_created
    lb.name = 'name'
    impl.list_loadbalancers.return_value = []
    impl.create_loadbalancer.return_value = {'id': '1234',
                                             'vip_address': '1.1.1.1',
                                             'vip_port_id': '4321'}
    impl.show_loadbalancer.return_value = {'provisioning_status': 'ACTIVE'}
    impl.show_pool.return_value = {'provisioning_status': 'ACTIVE'}
    impl.find_secgrp.return_value = 'sg_id'
    impl.list_listeners.return_value = []
    impl.list_pools.return_value = []
    impl.list_members.return_value = ['members']
    impl.list_sg_rules.return_value = []
    impl.get_port_sec_enabled.return_value = True
    lb.create()
    assert lb.sg_id is None
    assert lb.fip is None
    assert lb.address == '1.1.1.1'
    assert lb.members == ['members']
    assert lb.is_created
    assert impl.create_loadbalancer.called
    assert not impl.create_secgrp.called
    impl.create_sg_rule.assert_called_with('sg_id', '1.1.1.1')
    assert not impl.set_port_secgrp.called
    assert impl.create_listener.called
    assert impl.create_pool.called
    assert not impl.list_fips.called
    assert not impl.create_fip.called

    impl.find_secgrp.return_value = None
    with pytest.raises(openstack.OpenStackLBError):
        lb.create()
    log_err.assert_called_with('Unable to find default security group')

    lb.fip_net = 'net'
    lb.manage_secgrps = True
    impl.create_secgrp.return_value = 'sg_id'
    impl.list_fips.return_value = []
    lb.create()
    assert lb.sg_id == 'sg_id'
    impl.create_secgrp.assert_called_with('name')
    impl.set_port_secgrp.assert_called_with('4321', 'sg_id')
    impl.create_fip.assert_called_with('1.1.1.1', '4321')


def test_create_recover(impl, sleep):
    openstack.kv().get.return_value = None
    lb = openstack.LoadBalancer('app', '80', 'subnet', 'alg', 'net', True)
    lb.name = 'name'
    impl.list_loadbalancers.return_value = [{'name': 'name'}]
    impl.show_loadbalancer.return_value = {'id': '2345',
                                           'provisioning_status': 'ACTIVE',
                                           'vip_address': '1.1.1.1',
                                           'vip_port_id': '4321'}
    impl.find_secgrp.return_value = 'sg_id'
    impl.list_sg_rules.return_value = [{'Port Range': '', 'IP Range': ''}]
    impl.get_port_sec_enabled.return_value = False
    impl.list_listeners.return_value = [{'name': 'name'}]
    impl.list_pools.return_value = [{'name': 'name'}]
    impl.list_fips.return_value = [
        {'Fixed IP Address': '2.2.2.2', 'Floating IP Address': '3.3.3.3'},
        {'Fixed IP Address': '1.1.1.1', 'Floating IP Address': '4.4.4.4'},
    ]
    impl.list_members.return_value = ['members']
    lb.create()
    assert lb.sg_id == 'sg_id'
    assert lb.fip == '4.4.4.4'
    assert lb.address == '1.1.1.1'
    assert lb.members == ['members']
    assert lb.is_created
    assert not impl.create_loadbalancer.called
    assert not impl.create_secgrp.called
    assert not impl.create_listener.called
    assert not impl.create_pool.called
    assert not impl.create_fip.called


def test_wait_not_pending(impl, sleep):
    lb = openstack.LoadBalancer('app', '80', 'subnet', 'alg', None, False)
    test_func = mock.Mock(side_effect=[
        {'provisioning_status': 'PENDING_CREATE'},
        {'provisioning_status': 'PENDING_UPDATE'},
        {'provisioning_status': 'PENDING_DELETE'},
        {'provisioning_status': 'ACTIVE'},
    ])
    lb._wait_not_pending(test_func)
    assert sleep.call_count == 3

    test_func = mock.Mock(return_value={
        'provisioning_status': 'PENDING_DELETE',
    })
    with pytest.raises(openstack.OpenStackLBError):
        lb._wait_not_pending(test_func)


def test_find_matching_sg_rule(impl):
    lb = openstack.LoadBalancer('app', '80', 'subnet', 'alg', None, False)
    lb.address = '1.1.1.1'

    impl.list_sg_rules.return_value = [{'Port Range': None,
                                        'IP Range': None}]
    assert lb._find_matching_sg_rule('sg_id')

    impl.list_sg_rules.return_value = [{'Port Range': '60:90',
                                        'IP Range': ''}]
    assert lb._find_matching_sg_rule('sg_id')

    impl.list_sg_rules.return_value = [{'Port Range': '',
                                        'IP Range': '1.0.0.0/8'}]
    assert lb._find_matching_sg_rule('sg_id')

    impl.list_sg_rules.return_value = [{'Port Range': '81:90',
                                        'IP Range': ''},
                                       {'Port Range': '',
                                        'IP Range': '2.0.0.0/8'}]
    assert not lb._find_matching_sg_rule('sg_id')

    impl.list_sg_rules.return_value = []
    assert not lb._find_matching_sg_rule('sg_id')


def test_find(impl, log_err):
    lb = openstack.LoadBalancer('app', '80', 'subnet', 'alg', None, False)
    lb.name = 'lb'
    item1 = {'id': 1, 'name': 'not-lb'}
    item2 = {'id': 2, 'name': 'lb'}
    item3 = {'id': 3, 'name': 'lb'}
    assert lb._find('foo', [item1]) is None
    assert lb._find('foo', [item1, item2]) == item2
    with pytest.raises(openstack.OpenStackLBError):
        lb._find('foo', [item1, item2, item3])
    log_err.assert_called_with('Multiple {} found: {}', 'foo', 'lb')


def test_update_members(impl):
    lb = openstack.LoadBalancer('app', '80', 'subnet', 'alg', None, False)
    impl.show_pool.return_value = {'provisioning_status': 'ACTIVE'}
    lb.members = {(1, 2), (3, 4)}
    lb.update_members({(1, 2), (3, 4)})
    assert not impl.delete_member.called
    assert not impl.create_member.called

    lb.members = {(1, 2), (3, 4)}
    lb.update_members({(1, 2), (3, 4), (5, 6)})
    assert not impl.delete_member.called
    assert impl.create_member.called
    assert lb.members == {(1, 2), (3, 4), (5, 6)}

    impl.create_member.reset_mock()
    lb.members = {(1, 2), (3, 4)}
    lb.update_members({(1, 2)})
    assert impl.delete_member.called
    assert not impl.create_member.called

    impl.delete_member.reset_mock()
    lb.members = {(1, 2), (3, 4)}
    lb.update_members({(5, 6)})
    assert impl.delete_member.called
    assert impl.create_member.called

    impl.delete_member.side_effect = CalledProcessError(1, 'cmd')
    lb.members = {(1, 2), (3, 4)}
    with pytest.raises(openstack.OpenStackLBError):
        lb.update_members(set())

    impl.delete_member.side_effect = AssertionError('should not be called')
    impl.create_member.side_effect = CalledProcessError(1, 'cmd')
    lb.members = set()
    with pytest.raises(openstack.OpenStackLBError):
        lb.update_members({(1, 2)})


def test_is_base64():
    cert = ('-----BEGIN CERTIFICATE-----\nMIIDITCCAgmgAwIBAgIUeQxHSsZt6auk1oW+'
            'SRFXC4T6nNcwDQYJKoZIhvcNAQEL\nBQAwIDELMAkGA1UEBhMCVUsxETAPBgNVBAo'
            'MCElubWFyc2F0MB4XDTE5MTEwNDE1\nMTQzOFoXDTI5MTEwMTE1MTQzOFowIDELMA'
            'kGA1UEBhMCVUsxETAPBgNVBAoMCElu\nbWFyc2F0MIIBIjANBgkqhkiG9w0BAQEFA'
            'AOCAQ8AMIIBCgKCAQEA09qCmv8jF+N1\ndl/ae3VQV95FG7WFrjS6fbZ1TpXkO9Vs'
            'PKhA9lRUBxs58noKIkMIUeXYy4wvSu28\nX67NqB2bv3iyns/mEzPYE1GxtFXIPhk'
            'KO22vqVLZ0CFAuV47AhqDOXtyqwwfxoBT\nKxMi430UCb+3cPaev/mZMlvf6iJfdi'
            'hyPfMEwtIanS/QKgEvykhP1kAZ36ActFmK\nWnJtjBBFUKQIBQzguMTqUXX7wvwRe'
            'gK8lgXiZ6iZiOza0C7hSdBVylcKeaqoLnP5\nW93m3YZTXc08A30PieTJQFD6Bm+4'
            '1Kv2FxQAXjRnCzvIJL44zJXjLmnUdZbSzdl8\nPpu3wJu9cQIDAQABo1MwUTAdBgN'
            'VHQ4EFgQUwQsYIyqud2WQkAlcDwIuu7nAvnYw\nHwYDVR0jBBgwFoAUwQsYIyqud2'
            'WQkAlcDwIuu7nAvnYwDwYDVR0TAQH/BAUwAwEB\n/zANBgkqhkiG9w0BAQsFAAOCA'
            'QEAn5oQYeyaxcqOjzUxbkEy4pOJMg/nTKkt+8yh\nFSqUv1Vc3HGg65uGq08eJDq9'
            'AP7PrfvSQJWQpFBS80bNN8idCmhMutpA8X6+Z0wv\n0p5dzQFAUdSLLN0so4iXKtP'
            'k5wp0r84W0xbqWPRWRSw+lCe1WrMK+ARDpPv+AxOW\nf7JFQkqzEsWu6RCjy0KobO'
            'y7PPq17wXEhXynNcMAXjQe9DkTBb34K6PYku1Ftxfr\n3IRWaSrDB9BJTje6/tmz7'
            'IcO8ss+Y3gUZeaqTLdZz8RJUlJqNqfdTQif2hKLYjro\nBwZYRQo8TkDmSlz00LwQ'
            'So1xLX27nGHB621pgNCZbJMKvZOrQg==\n-----END CERTIFICATE-----\n')
    cert = cert.encode('utf8')
    assert openstack._is_base64(cert) is False
    # Base64 encoded foobar string
    foobar = 'Zm9vYmFyCg==\n'.encode('utf8')
    assert openstack._is_base64(foobar)
