"""igvm - Integration Tests

Copyright (c) 2018 InnoGames GmbH
"""

from __future__ import print_function

from logging import INFO, basicConfig
from os import environ
from pipes import quote
from tempfile import NamedTemporaryFile
from unittest import TestCase
from uuid import uuid4

from adminapi.dataset import Query
from fabric.api import env

from igvm.commands import (
    disk_set,
    host_info,
    mem_set,
    vcpu_set,
    vm_build,
    vm_delete,
    vm_restart,
    vm_start,
    vm_stop,
    vm_sync,
    _get_vm,
)
from igvm.exceptions import (
    IGVMError,
    InvalidStateError,
    InconsistentAttributeError,
)
from igvm.hypervisor import Hypervisor
from igvm.migratevm import migratevm
from igvm.settings import (
    COMMON_FABRIC_SETTINGS,
    HYPERVISOR_ATTRIBUTES,
    IMAGE_PATH,
    VG_NAME,
)
from igvm.utils import parse_size
from fabric.network import disconnect_all

basicConfig(level=INFO)
env.update(COMMON_FABRIC_SETTINGS)
env['user'] = 'igtesting'  # Enforce user for integration testing process
environ['IGVM_MODE'] = 'testing'

# Configuration of VMs used for tests
# Keep in mind that the whole hostname must fit in 64 characters.
VM_HOSTNAME = 'igvm-{}.test.ig.local'.format(uuid4())
VM_NET = 'igvm-net-aw.test.ig.local'


def setUpModule():
    # Automatically find suitable HVs for tests.
    # Terminate if this is impossible - we can't run tests without HVs.
    global HYPERVISORS
    vm_route_net = (
        Query({'hostname': VM_NET}, ['route_network']).get()['route_network']
    )

    # We can access HVs as objects but that does not mean we can compare them
    # to any objects returned from igvm - those will be different objects,
    # created from scratch from Serveradmin data.
    HYPERVISORS = [Hypervisor(o) for o in Query({
        'servertype': 'hypervisor',
        'environment': 'testing',
        'vlan_networks': vm_route_net,
        'state': 'online',
    }, HYPERVISOR_ATTRIBUTES)]

    if len(HYPERVISORS) < 2:
        raise Exception('Not enough testing hypervisors found')

    query = Query()
    vm_obj = query.new_object('vm')
    vm_obj['hostname'] = VM_HOSTNAME
    vm_obj['intern_ip'] = Query(
        {'hostname': VM_NET}, ['intern_ip']
    ).get_free_ip_addrs()
    vm_obj['project'] = 'test'
    vm_obj['team'] = 'test'

    query.commit()


def tearDownModule():
    query = Query({'hostname': VM_HOSTNAME}, ['hostname'])
    for obj in query:
        obj.delete()
    query.commit()
    disconnect_all()  # Will hang on Jessie + Python3


def cmd(cmd, *args, **kwargs):
    escaped_args = [quote(str(arg)) for arg in args]

    escaped_kwargs = {}
    for key, value in kwargs.items():
        escaped_kwargs[key] = quote(str(value))

    return cmd.format(*escaped_args, **escaped_kwargs)


class IGVMTest(TestCase):
    def setUp(self):
        """Initialize VM object before every test

        Get object from Serveradmin and initialize it to safe defaults.
        Don't assign VM to any of HVs yet!
        """
        # igvm operates always on hostname of VM and queries it from
        # Serveradmin whenever it needs. Because of that we must never store
        # any igvm objects and query things anew each time.
        obj = Query({'hostname': VM_HOSTNAME}, [
            'state',
            'backup_disabled',
            'disk_size_gib',
            'memory',
            'num_cpu',
            'os',
            'environment',
            'no_monitoring',
            'hypervisor',
            'repositories',
            'puppet_environment',
        ]).get()

        # Fill in defaults in Serveradmin
        obj['state'] = 'online'
        obj['disk_size_gib'] = 3
        obj['memory'] = 2048
        obj['num_cpu'] = 2
        obj['os'] = 'jessie'
        obj['environment'] = 'testing'
        obj['no_monitoring'] = True
        obj['hypervisor'] = None
        obj['repositories'] = [
            'int:basejessie:stable',
            'int:innogames:stable jessie',
        ]
        obj['puppet_environment'] = None
        obj['backup_disabled'] = True
        obj.commit()

    def tearDown(self):
        """Clean up all HVs after every test"""

        vm_obj = Query({'hostname': VM_HOSTNAME}, ['hostname']).get()
        uid_name = '{}_{}'.format(vm_obj['object_id'], vm_obj['hostname'])

        for hv in HYPERVISORS:
            hv.run(
                'virsh destroy {vm}; '
                'virsh undefine {vm}; '
                'umount /dev/{vg}/{vm}; '
                'lvremove -f /dev/{vg}/{vm}'
                .format(vg=VG_NAME, vm=uid_name),
                warn_only=True,
            )

    def check_vm_present(self):
        # Operate on fresh object
        with _get_vm(VM_HOSTNAME) as vm:

            for hv in HYPERVISORS:
                if hv.dataset_obj['hostname'] == vm.dataset_obj['hypervisor']:
                    # Is it on correct HV?
                    self.assertEqual(hv.vm_defined(vm), True)
                    self.assertEqual(hv.vm_running(vm), True)
                else:
                    # Is it gone from other HVs after migration?
                    self.assertEqual(hv.vm_defined(vm), False)
                    hv.run('test ! -b /dev/{}/{}'.format(VG_NAME, vm.fqdn))

            # Is VM itself alive and fine?
            fqdn = vm.run('hostname -f').strip()
            self.assertEqual(fqdn, vm.fqdn)
            self.assertEqual(vm.dataset_obj.is_dirty(), False)

    def check_vm_absent(self, hv_name=None):
        # Operate on fresh object
        with _get_vm(VM_HOSTNAME) as vm:
            if not hv_name:
                hv_name = vm.dataset_obj['hypervisor']

            if not hv_name:
                hv_name = vm.dataset_obj['hypervisor']

            for hv in HYPERVISORS:
                if hv.dataset_obj['hostname'] == hv_name:
                    self.assertEqual(hv.vm_defined(vm), False)
                    hv.run('test ! -b /dev/{}/{}'.format(VG_NAME, vm.fqdn))


class BuildTest(IGVMTest):
    """Test many possible VM building scenarios"""

    def setUp(self):
        super(BuildTest, self).setUp()
        # Normally build tests happen on the 1st HV
        obj = Query({'hostname': VM_HOSTNAME}, ['hypervisor']).get()
        obj['hypervisor'] = HYPERVISORS[0].dataset_obj['hostname']
        obj.commit()
        with _get_vm(VM_HOSTNAME) as vm:
            # For contacting VM over shell
            self.vm = vm

    def test_build(self):
        vm_build(VM_HOSTNAME)
        self.check_vm_present()

    def test_build_auto_find_hypervisor(self):
        # HV is configured for all BuildTest class tests by default.
        # But this test requires it unconfigured.
        obj = Query({'hostname': VM_HOSTNAME}, ['hypervisor']).get()
        obj['hypervisor'] = None
        obj.commit()
        vm_build(VM_HOSTNAME)
        self.check_vm_present()

    def test_build_stretch(self):
        obj = Query({'hostname': VM_HOSTNAME}, ['os', 'repositories']).get()
        obj.update({
            'os': 'stretch',
            'repositories': [
                'int:basestretch:stable',
                'int:innogames:stable stretch',
            ]
        })
        obj.commit()
        vm_build(VM_HOSTNAME)

        self.check_vm_present()

    def test_postboot(self):
        with NamedTemporaryFile() as fd:
            fd.write('echo hello > /root/postboot_result'.encode())
            fd.flush()

            vm_build(VM_HOSTNAME, postboot=fd.name)
            self.check_vm_present()

            output = self.vm.run('cat /root/postboot_result')
            self.assertIn('hello', output)

    def test_delete(self):
        vm_build(VM_HOSTNAME)
        self.check_vm_present()

        # Fails while VM is powered on
        with self.assertRaises(IGVMError):
            vm_delete(VM_HOSTNAME)

        self.vm.shutdown()
        vm_delete(VM_HOSTNAME, retire=True)

        self.check_vm_absent()

    def test_rollback(self):
        obj = Query({'hostname': VM_HOSTNAME}, ['puppet_environment']).get()
        obj['puppet_environment'] = 'doesnotexist'
        obj.commit()

        with self.assertRaises(IGVMError):
            vm_build(VM_HOSTNAME)

        self.check_vm_absent()

    def test_rebuild(self):
        vm_build(VM_HOSTNAME)

        # Build the VM again, this must fail, as it is already built
        with self.assertRaises(IGVMError):
            vm_build(VM_HOSTNAME)

        # Create files on VM to check later if the VM was really rebuilt
        self.vm.run('touch /root/initial_canary')
        self.vm.run('test -f /root/initial_canary')

        # Now stop it and rebuild it
        vm_stop(VM_HOSTNAME)
        vm_build(VM_HOSTNAME, rebuild=True)
        self.check_vm_present()

        # The VM was rebuild and thus the test file must be gone
        self.vm.run('test ! -f /root/initial_canary')


class CommandTest(IGVMTest):
    def setUp(self):
        super(CommandTest, self).setUp()
        # For every command test build a VM on the 1st HV
        obj = Query({'hostname': VM_HOSTNAME}, ['hypervisor']).get()
        obj['hypervisor'] = HYPERVISORS[0].dataset_obj['hostname']
        obj.commit()
        vm_build(VM_HOSTNAME)
        self.check_vm_present()
        with _get_vm(VM_HOSTNAME) as vm:
            # For contacting VM over shell
            self.vm = vm

    def test_start_stop(self):
        # Doesn't fail, but should print a message
        vm_start(VM_HOSTNAME)
        self.check_vm_present()

        vm_restart(VM_HOSTNAME)
        self.check_vm_present()

        vm_stop(VM_HOSTNAME)
        self.assertEqual(self.vm.is_running(), False)

        vm_start(VM_HOSTNAME)
        self.assertEqual(self.vm.is_running(), True)

        vm_stop(VM_HOSTNAME, force=True)
        self.assertEqual(self.vm.is_running(), False)
        vm_start(VM_HOSTNAME)

        vm_restart(VM_HOSTNAME, force=True)
        self.check_vm_present()

    def test_disk_set(self):
        def _get_hv():
            return (
                self.vm.hypervisor.vm_sync_from_hypervisor(self.vm)
                ['disk_size_gib']
            )

        def _get_vm():
            return parse_size(
                self.vm.run("df -h / | tail -n+2 | awk '{ print $2 }'")
                .strip(),
                'G'
            )

        # Initial size same as built
        obj = Query({'hostname': VM_HOSTNAME}, ['disk_size_gib']).get()
        size = obj['disk_size_gib']
        self.assertEqual(_get_hv(), size)
        self.assertEqual(_get_vm(), size)

        size = size + 1
        disk_set(VM_HOSTNAME, '+1')
        obj = Query({'hostname': VM_HOSTNAME}, ['disk_size_gib']).get()

        self.assertEqual(obj['disk_size_gib'], size)
        self.assertEqual(_get_hv(), size)
        self.assertEqual(_get_vm(), size)

        size = 8
        disk_set(VM_HOSTNAME, '{}GB'.format(size))
        obj = Query({'hostname': VM_HOSTNAME}, ['disk_size_gib']).get()

        self.assertEqual(obj['disk_size_gib'], size)
        self.assertEqual(_get_hv(), size)
        self.assertEqual(_get_vm(), size)

        with self.assertRaises(Warning):
            disk_set(VM_HOSTNAME, '{}GB'.format(size))

        with self.assertRaises(NotImplementedError):
            disk_set(VM_HOSTNAME, '{}GB'.format(size - 1))

        with self.assertRaises(NotImplementedError):
            disk_set(VM_HOSTNAME, '-1')

    def test_mem_set(self):
        def _get_mem_hv():
            data = self.vm.hypervisor.vm_sync_from_hypervisor(self.vm)
            return data['memory']

        def _get_mem_vm():
            return float(
                self.vm
                .run("cat /proc/meminfo | grep MemTotal | awk '{ print $2 }'")
                .strip()
            ) // 1024

        # Online
        self.assertEqual(_get_mem_hv(), 2048)
        vm_mem = _get_mem_vm()
        mem_set(VM_HOSTNAME, '+1G')
        self.assertEqual(_get_mem_hv(), 3072)
        self.assertEqual(_get_mem_vm() - vm_mem, 1024)

        with self.assertRaises(Warning):
            mem_set(VM_HOSTNAME, '3G')

        with self.assertRaises(InvalidStateError):
            mem_set(VM_HOSTNAME, '2G')

        with self.assertRaises(IGVMError):
            mem_set(VM_HOSTNAME, '200G')

        # Not dividable
        with self.assertRaises(IGVMError):
            mem_set(VM_HOSTNAME, '4097M')

        self.assertEqual(_get_mem_hv(), 3072)
        vm_mem = _get_mem_vm()
        self.vm.shutdown()

        with self.assertRaises(IGVMError):
            mem_set(VM_HOSTNAME, '200G')

        mem_set(VM_HOSTNAME, '1024M')
        self.assertEqual(_get_mem_hv(), 1024)

        mem_set(VM_HOSTNAME, '2G')
        self.assertEqual(_get_mem_hv(), 2048)
        self.vm.start()
        self.assertEqual(_get_mem_vm() - vm_mem, -1024)

    def test_vcpu_set(self):
        def _get_hv():
            data = self.vm.hypervisor.vm_sync_from_hypervisor(self.vm)
            return data['num_cpu']

        def _get_vm():
            return int(
                self.vm.run('cat /proc/cpuinfo | grep vendor_id | wc -l')
                .strip()
            )

        # Online
        self.assertEqual(_get_hv(), 2)
        self.assertEqual(_get_vm(), 2)
        obj = Query({'hostname': VM_HOSTNAME}, ['num_cpu']).get()
        self.assertEqual(obj['num_cpu'], 2)
        vcpu_set(VM_HOSTNAME, 3)
        self.assertEqual(_get_hv(), 3)
        self.assertEqual(_get_vm(), 3)

        obj = Query({'hostname': VM_HOSTNAME}, ['num_cpu']).get()
        self.assertEqual(obj['num_cpu'], 3)

        with self.assertRaises(Warning):
            vcpu_set(VM_HOSTNAME, 3)

        # Online reduce not implemented yet on KVM
        with self.assertRaises(IGVMError):
            vcpu_set(VM_HOSTNAME, 2)

        # Offline
        vcpu_set(VM_HOSTNAME, 2, offline=True)
        self.assertEqual(_get_hv(), 2)
        self.assertEqual(_get_vm(), 2)

        # Impossible amount
        with self.assertRaises(IGVMError):
            vcpu_set(VM_HOSTNAME, 9001)

        with self.assertRaises(IGVMError):
            vcpu_set(VM_HOSTNAME, 0, offline=True)

        with self.assertRaises(IGVMError):
            vcpu_set(VM_HOSTNAME, -5)

        with self.assertRaises(IGVMError):
            vcpu_set(VM_HOSTNAME, -5, offline=True)

    def test_sync(self):
        obj = (
            Query({'hostname': VM_HOSTNAME}, ['disk_size_gib', 'memory']).get()
        )
        expected_disk_size = obj['disk_size_gib']
        obj['disk_size_gib'] += 10

        expected_memory = obj['memory']
        obj['memory'] += 1024

        obj.commit()

        vm_sync(VM_HOSTNAME)

        obj = (
            Query({'hostname': VM_HOSTNAME}, ['disk_size_gib', 'memory']).get()
        )
        self.assertEqual(obj['memory'], expected_memory)
        self.assertEqual(obj['disk_size_gib'], expected_disk_size)

        # Shouldn't do anything, but also shouldn't fail
        vm_sync(VM_HOSTNAME)

    def test_info(self):
        host_info(VM_HOSTNAME)
        self.vm.shutdown()
        host_info(VM_HOSTNAME)


class MigrationTest(IGVMTest):
    def setUp(self):
        super(MigrationTest, self).setUp()
        # Every migration gets a freshly built VM on the 1st HV
        obj = Query({'hostname': VM_HOSTNAME}, ['hypervisor']).get()
        obj['hypervisor'] = HYPERVISORS[0].dataset_obj['hostname']
        obj.commit()
        vm_build(VM_HOSTNAME)
        # And is performed to the 2nd HV
        # Of course apart from migrations to automatically selected HVs
        self.new_hv_name = HYPERVISORS[1].dataset_obj['hostname']

    def test_online_migration(self):
        migratevm(VM_HOSTNAME, self.new_hv_name)
        self.check_vm_present()

    def test_online_migration_auto_find_hypervisor(self):
        # auto find means no target HV is specified
        migratevm(VM_HOSTNAME)
        self.check_vm_present()

    def test_offline_migration_netcat(self):
        migratevm(
            VM_HOSTNAME, self.new_hv_name, offline=True,
            offline_transport='netcat',
        )
        self.check_vm_present()

    def test_offline_migration_drbd(self):
        migratevm(
            VM_HOSTNAME, self.new_hv_name, offline=True,
            offline_transport='drbd',
        )

        self.check_vm_present()

    def test_reject_out_of_sync_serveradmin(self):
        obj = Query({'hostname': VM_HOSTNAME}, ['disk_size_gib']).get()
        obj['disk_size_gib'] += 1
        obj.commit()

        with self.assertRaises(InconsistentAttributeError):
            migratevm(VM_HOSTNAME, self.new_hv_name)

    def test_reject_online_with_new_ip(self):
        with self.assertRaises(IGVMError):
            # Fake IP address is fine, this is a failing test.
            migratevm(VM_HOSTNAME, self.new_hv_name, newip='1.2.3.4')

    def test_reject_new_ip_without_puppet(self):
        with self.assertRaises(IGVMError):
            # Fake IP address is fine, this is a failing test.
            migratevm(
                VM_HOSTNAME,
                self.new_hv_name,
                offline=True,
                newip='1.2.3.4',
            )

    def test_new_ip(self):
        # We don't have a way to ask for new IP address from Serveradmin
        # and lock it for us. The method below will usually work fine.
        # When it starts failing, we must develop retry method.
        new_address = next(
            Query({'hostname': VM_NET}, ['intern_ip']).get_free_ip_addrs()
        )

        migratevm(
            VM_HOSTNAME,
            self.new_hv_name,
            offline=True,
            newip=new_address,
            run_puppet=True,
        )

        obj = Query({'hostname': VM_HOSTNAME}, ['intern_ip']).get()
        self.assertEqual(obj['intern_ip'], new_address)
        with _get_vm(VM_HOSTNAME) as vm:
            vm.run(cmd('ip a | grep {}', new_address))
        self.check_vm_present()

    def test_reject_online_with_puppet(self):
        with self.assertRaises(IGVMError):
            migratevm(VM_HOSTNAME, self.new_hv_name, run_puppet=True)

    def test_rollback_netcat(self):
        obj = Query({'hostname': VM_HOSTNAME}, ['puppet_environment']).get()
        obj['puppet_environment'] = 'doesnotexist'
        obj.commit()

        with self.assertRaises(IGVMError):
            migratevm(
                VM_HOSTNAME,
                self.new_hv_name,
                offline=True,
                run_puppet=True,
                offline_transport='netcat',
            )

        self.check_vm_present()
        self.check_vm_absent(self.new_hv_name)

    def test_rollback_drbd(self):
        obj = Query({'hostname': VM_HOSTNAME}, ['puppet_environment']).get()
        obj['puppet_environment'] = 'doesnotexist'
        obj.commit()

        with self.assertRaises(IGVMError):
            migratevm(
                VM_HOSTNAME,
                self.new_hv_name,
                offline=True,
                run_puppet=True,
                offline_transport='drbd',
            )

        self.check_vm_present()
        self.check_vm_absent(self.new_hv_name)
