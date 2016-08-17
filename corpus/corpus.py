import Pyro4
import os
import MySQLdb as mysql
import sys
import json
import time
import redis
import ssl

# For email
import smtplib
from email.mime.text import MIMEText

# Disable insecure platform warnings
import requests
requests.packages.urllib3.disable_warnings()

# For VMware
from pyVmomi import vim
from pyVmomi import vmodl
from pyVim.connect import SmartConnect, Disconnect

# For checking if a cache_vm task is currently running
# we talk about to neocortex via RPC
import Pyro4

class Corpus(object):
	"""Library functions used in both cortex and neocortex and workflow tasks"""

	OS_TYPE_BY_ID   = {0: "None", 1: "Linux", 2: "Windows", 3: "ESXi", 4: "Solaris" }
	OS_TYPE_BY_NAME = {"None": 0, "Linux": 1, "Windows": 2, "ESXi": 3, "Solaris": 4 }
	SYSTEM_TYPE_BY_ID = {0: "System", 1: "Legacy", 2: "Other"}
	SYSTEM_TYPE_BY_NAME = {"System": 0, "Legacy": 1, "Other": 2}

	class TaskFatalError(Exception):
		def __init__(self, message="The task failed for an unspecified reason"):
			self.message = str(message)

		def __str__(self):
			return self.message

	class VMwareTaskError(Exception):
		def __init__(self, message="An error was returned from vmware"):
			self.message = str(message)

		def __str__(self):
			return self.message

	def __init__(self, db, config):
		self.db     = db
		self.config = config
		self.rdb    = self._connect_redis()

	################################################################################

	def allocate_name(self, class_name, comment, username, num=1, expiry=None):
		"""Allocates 'num' systems, of type 'class_name' each with the given
		comment. Returns a dictionary with mappings between each new name
		and the corresponding row ID in the database table."""

		# dictionary of new systems
		new_systems = {}

		# Get a cursor to the database
		cur = self.db.cursor(mysql.cursors.DictCursor)

		# The following MySQL code is a bit complicated but as an explanation:
		#  * The classes table contains the number of the next system in each
		#    class to allocate.
		#  * To prevent race conditions when allocating (and thus so two
		#    systems don't get allocated with the same number) we lock the 
		#    tables we want to view/modify during this function.
		#  * This prevents other simultanteously running calls to this function
		#    from allocating a name temporarily (the call to LOCK TABLE will
		#    block/hang until the tables become unlocked again).
		#  * Once a lock is aquired, we get the next number to allocate from
		#    the classes table, update that table with the next new number,
		#    based on how many names we're simultaneously allocating, and then
		#    create the systems in the systems table.
		#  * This is all commited as one transaction so either this all happens
		#    or none of it happens (i.e. in case of an error)
		#  * In all scenarios, we end by unlocking the table so other calls to
		#    this function can carry on.

		## 1. Lock the table to prevent other requests issuing names whilst we are
		cur.execute('LOCK TABLE `classes` WRITE, `systems` WRITE; ')

		# 2a. Get the class (along with the next nubmer to allocate)
		try:
			cur.execute("SELECT * FROM `classes` WHERE `name` = %s",(class_name))
			class_data = cur.fetchone()
		except Exception as ex:
			cur.execute('UNLOCK TABLES;')
			raise Exception("Selected system class does not exist: cannot allocate system name")

		# 2b. Ensure the class was found and that it is not disabled
		if class_data == None:
			cur.execute('UNLOCK TABLES;')
			raise Exception("Selected system class does not exist: cannot allocate system name")
		elif int(class_data['disabled']) == 1:
			cur.execute('UNLOCK TABLES;')
			raise Exception("Selected system class has been disabled: cannot allocate: cannot allocate system name")

		try:
			## 3. Increment the number by the number we're simultaneously allocating
			cur.execute("UPDATE `classes` SET `lastid` = %s WHERE `name` = %s", (int(class_data['lastid']) + int(num), class_name))

			## 4. Create the appropriate number of servers
			for i in xrange(1, num+1):
				new_number = int(class_data['lastid']) + i
				new_name   = self.pad_system_name(class_name, new_number, class_data['digits'])

				cur.execute("INSERT INTO `systems` (`type`, `class`, `number`, `name`, `allocation_date`, `allocation_who`, `allocation_comment`, `expiry_date`) VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s)",
					(self.SYSTEM_TYPE_BY_NAME['System'], class_name, new_number, new_name, username, comment, expiry))

				# store a record of the new system so we can give this back to the browser in a minute
				new_systems[new_name] = cur.lastrowid

			## 5. All names are now created and the table incremented. Time to commit.
			self.db.commit()
		except Exception as ex:
			cur.execute('UNLOCK TABLES;')
			raise ex

		## 6. Finally, unlock the tables so others can allocate
		cur.execute('UNLOCK TABLES;')

		return new_systems

	################################################################################

	def pad_system_name(self, prefix, number, digits):
		"""Takes a class name ('prefix') a system number, and the number of
		digits that class should have in its name and formats a string to that
		specification. For example, if prefix is 'test', number is '12' and
		'digits' is 5, then this returns 'test00012'"""

		return ("%s%0" + str(int(digits)) + "d") % (prefix, number)	

	################################################################################

	def infoblox_create_host(self, name, subnet):
		payload = {'name': name, "ipv4addrs": [{"ipv4addr":"func:nextavailableip:" + subnet}],}
		r = requests.post("https://" + self.config['INFOBLOX_HOST'] + "/wapi/v2.0/record:host", data=json.dumps(payload), auth=(self.config['INFOBLOX_USER'], self.config['INFOBLOX_PASS']))

		if r.status_code == 201:
			objectid = str(r.json())
			r = requests.get("https://" + self.config['INFOBLOX_HOST'] + "/wapi/v2.0/" + objectid, auth=(self.config['INFOBLOX_USER'], self.config['INFOBLOX_PASS']))
	
			if r.status_code == 200:
				response = r.json()

				try:
					return response['ipv4addrs'][0]['ipv4addr']
				except Exception as ex:
					raise RuntimeError("Malformed JSON response from Infoblox API")
			else:
				raise RuntimeError("Error returned from Infoblox API. Code " + str(r.status_code) + ": " + r.text)
		else:
			raise RuntimeError("Error returned from Infoblox API. Code " + str(r.status_code) + ": " + r.text)

	################################################################################

	def infoblox_get_host_refs(self,fqdn):
		"""Returns a list of host references (Infoblox record IDs) from Infoblox
		matching exactly the specified fully qualified domain name (FQDN). If no
		records are found None is returned. If an error occurs LookupError is raised"""

		payload = {'name:': fqdn}
		r = requests.get("https://" + self.config['INFOBLOX_HOST'] + "/wapi/v2.0/record:host", data=json.dumps(payload), auth=(self.config['INFOBLOX_USER'], self.config['INFOBLOX_PASS']))

		results = []

		if r.status_code == 200:
			response = r.json()

			if isinstance(response, list):
				if len(response) == 0:
					return None
				else:
					for record in response:
						if '_ref' in record:
							results.append(record['_ref'])
				
				return results
			else:
				raise LookupError("Invalid data returned from Infoblox API. Code " + str(r.status_code) + ": " + r.text)			
		else:
			raise LookupError("Error returned from Infoblox API. Code " + str(r.status_code) + ": " + r.text)

	################################################################################

	def vmware_get_obj(self, content, vimtype, name):
		"""
		Return an object by name, if name is None the
		first found object is returned. Searches within
		content.rootFolder (i.e. everything on the vCenter)
		"""

		return self.vmware_get_obj_within_parent(content, vimtype, name, content.rootFolder)

	################################################################################

	def vmware_get_obj_within_parent(self, content, vimtype, name, parent):
		"""
		Return an object by name, if name is None the
		first found object is returned. Set parent to be
		a Folder, Datacenter, ComputeResource or ResourcePool under
		which to search for the object.
		"""
		obj = None
		container = content.viewManager.CreateContainerView(parent, vimtype, True)
		for c in container.view:
			if name:
				if c.name == name:
					obj = c
					break
			else:
				obj = c
				break

		return obj

	################################################################################

	def vmware_task_wait(self, task):
		"""Waits for vCenter task to finish"""

		task_done = False

		while not task_done:
			if task.info.state == 'success':
				return True

			if task.info.state == 'error':
				return False

			## other states are 'queued' and 'running'
			## which we should just wait on.

			## lets not busy wait CPU 100%...
			time.sleep(1)

	################################################################################

	def vmware_task_complete(self, task, on_error="VMware API Task Failed"):
		"""
		Block until the given task is complete. An exception is 
		thrown if the task results in an error state. This function
		does not return a variable.
		"""

		task_done = False

		while not task_done:
			if task.info.state == 'success':
				return

			if task.info.state == 'error':
			
				## Try to get a meaningful error message
				if hasattr(task.info.error,'msg'):
					error_message = task.info.error.msg
				else:
					error_message = str(task.info.error)

				on_error = on_error + ': '

				raise RuntimeError(on_error + error_message)

			## If not success or error, then sleep a bit and check again.
			## Otherwise we just busywaitloop the CPU at 100% for no reason.
			time.sleep(1)

	################################################################################

	def vmware_vm_custspec(self, dhcp=True, gateway=None, netmask=None, ipaddr=None, dns_servers="8.8.8.8", dns_domain="localdomain", os_type=None, os_domain="localdomain", timezone=None, hwClockUTC=True, domain_join_user=None, domain_join_pass=None, fullname=None, orgname=None, productid=""):
		"""This function generates a vmware VM customisation spec for use in cloning a VM. 

		   If you choose DHCP (the default) the gateway, netmask, ipaddr, dns_servers and dns_domain parameters are ignored.

		   For Linux use these optional parameters:
		   os_domain - usually soton.ac.uk
		   hwClockUTC - usually True
		   timezone - usually 'Europe/London'

		   For Windows use these optional parameters:
		   timezone - numerical ID, usually 85 for UK.
		   os_domain   - the AD domain to join, usually soton.ac.uk
		   domain_join_user - the user to join AD with
		   domain_join_pass - the user's password
		   fullname - the fullname of the customer
		   orgname - the organisation name of the customer

		"""

		## global IP settings
		globalIPsettings = vim.vm.customization.GlobalIPSettings()

		## these are optional for DHCP
		if not dhcp:
			globalIPsettings.dnsSuffixList = [dns_domain]
			globalIPsettings.dnsServerList  = dns_servers

		## network settings master object
		ipSettings                   = vim.vm.customization.IPSettings()

		## the IP address
		if dhcp:
			ipSettings.ip            = vim.vm.customization.DhcpIpGenerator()
		else:
			fixedIP                  = vim.vm.customization.FixedIp()
			fixedIP.ipAddress        = ipaddr
			ipSettings.ip            = fixedIP
			ipSettings.dnsDomain     = dns_domain
			ipSettings.dnsServerList = dns_servers
			ipSettings.gateway       = [gateway]
			ipSettings.subnetMask    = netmask

		## Create the 'adapter mapping'
		adapterMapping            = vim.vm.customization.AdapterMapping()
		adapterMapping.adapter    = ipSettings

		# create the customisation specification
		custspec                  = vim.vm.customization.Specification()
		custspec.globalIPSettings = globalIPsettings
		custspec.nicSettingMap    = [adapterMapping]

		if os_type == self.OS_TYPE_BY_NAME['Linux']:

			linuxprep            = vim.vm.customization.LinuxPrep()
			linuxprep.domain     = os_domain
			linuxprep.hostName   = vim.vm.customization.VirtualMachineNameGenerator()
			linuxprep.hwClockUTC = hwClockUTC
			linuxprep.timeZone   = timezone

			## finally load in the sysprep into the customisation spec
			custspec.identity = linuxprep
	
		elif os_type == self.OS_TYPE_BY_NAME['Windows']:

			## the windows sysprep /CRI
			guiUnattended = vim.vm.customization.GuiUnattended()
			guiUnattended.autoLogon = False
			guiUnattended.autoLogonCount = 0
			guiUnattended.timeZone = timezone

			sysprepIdentity = vim.vm.customization.Identification()
			sysprepIdentity.domainAdmin = domain_join_user
			sysprepPassword = vim.vm.customization.Password()
			sysprepPassword.plainText = True
			sysprepPassword.value = domain_join_pass
			sysprepIdentity.domainAdminPassword = sysprepPassword
			sysprepIdentity.joinDomain = os_domain

			sysprepUserData = vim.vm.customization.UserData()
			sysprepUserData.computerName = vim.vm.customization.VirtualMachineNameGenerator()
			sysprepUserData.fullName = fullname
			sysprepUserData.orgName = orgname
			sysprepUserData.productId = productid

			sysprep                = vim.vm.customization.Sysprep()
			sysprep.guiUnattended  = guiUnattended
			sysprep.identification = sysprepIdentity
			sysprep.userData       = sysprepUserData

			## finally load in the sysprep into the customisation spec
			custspec.identity = sysprep

		else:
			raise Exception("Invalid os_type")

		return custspec

	################################################################################

	def vmware_smartconnect(self, tag):
		instance = self.config['VMWARE'][tag]

		sslContext = ssl.create_default_context()
		
		if 'verify' in instance:
			if instance['verify'] == False:
				sslContext.check_hostname = False
				sslContext.verify_mode = ssl.CERT_NONE

		return SmartConnect(host=instance['hostname'], user=instance['user'], pwd=instance['pass'], port=instance['port'], sslContext=sslContext)

	################################################################################
	
	def vmware_clone_vm(self, service_instance, vm_template, vm_name, vm_datacenter=None, vm_datastore=None, vm_folder=None, vm_cluster=None, vm_rpool=None, vm_network=None, vm_poweron=False, custspec=None, vm_datastore_cluster=None):
		"""This function connects to vcenter and clones a virtual machine. Only vm_template and
		   vm_name are required parameters although this is unlikely what you'd want - please
		   read the parameters and check if you want to use them.

		   If you want to customise the VM after cloning attach a customisation spec via the 
		   custspec optional parameter.
		"""

		need_config_spec = False
		config_spec = vim.vm.ConfigSpec()
		content = service_instance.RetrieveContent()

		## Get the template
		template = self.vmware_get_obj(content, [vim.VirtualMachine], vm_template)

		if template is None:
			raise RuntimeError("Failed to locate template, '" + vm_template + "'")

		## VMware datacenter - this is only used to get the folder
		datacenter = self.vmware_get_obj(content, [vim.Datacenter], vm_datacenter)

		if datacenter is None:
			raise RuntimeError("Failed to locate datacenter object")

		## VMware folder
		if vm_folder:
			destfolder = self.vmware_get_obj(content, [vim.Folder], vm_folder)

			if destfolder is None:
				raise RuntimeError("Failed to locate destination folder, '" + vm_folder + "'")
		else:
			destfolder = datacenter.vmFolder

		## Get the VMware Cluster
		cluster = self.vmware_get_obj(content, [vim.ClusterComputeResource], vm_cluster)

		if cluster is None:
			raise RuntimeError("Failed to locate destination cluster, '" + vm_cluster + "'")

		## You can't specify a cluster for a VM to be created on, instead you specify either a host or a resource pool.
		## We will thus create within a resource pool. 

		# If this function isn't passed a resource pool then choose the default:
		if vm_rpool == None:
			rpool = cluster.resourcePool

		else:
			## But if we are given a specific resource pool...
			## ...but we werent given a specific cluster, then just search for the resource pool name anywhere on the vCenter:
			if vm_cluster == None:
				rpool = self.vmware_get_obj(content, [vim.ResourcePool], vm_rpool)

			else:
				# ...but if we were given a specific cluster *and* a specific resource pool, then search for the resource pool within that cluster's resource pools:
				rpool = self.vmware_get_obj_within_parent(content, [vim.ResourcePool], vm_rpool, cluster.resourcePool)

			## If we didn't find a resource pool just use the default one for the cluster
			if rpool == None:
				rpool = cluster.resourcePool

		# If we're passed a new network:
		if vm_network is not None:
			need_config_spec = True

			# Search for the new network by name
			network = self.vmware_get_obj(content, [vim.DistributedVirtualPortgroup], vm_network)

			if network is None:
				raise RuntimeError("Failed to locate destination network, '" + vm_network + "'")

			# Iterate through the devices on the existing template
			network_device_found = False
			for device in template.config.hardware.device:
				# If we've found the network card (assumption: that there is only one!)
				if isinstance(device, vim.vm.device.VirtualEthernetCard):
					# For error detection
					network_device_found = True

					# Build a new NIC specification for the modified device, based on the one in the template
					nicspec = vim.vm.device.VirtualDeviceSpec()
					nicspec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
					nicspec.device = device

					# Create a new connection to a port on the dvSwitch
					dvs_port_connection = vim.dvs.PortConnection()
					dvs_port_connection.portgroupKey = network.key
					dvs_port_connection.switchUuid = network.config.distributedVirtualSwitch.uuid

					# Create a new device backing, changing it to the given vm_network
					nicspec.device.backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
					nicspec.device.backing.port = dvs_port_connection

			# Error checking
			if not network_device_found:
				raise RuntimeError("Failed to locate NIC on template")

			# Update the config spec
			config_spec.deviceChange.append(nicspec)

		## Start the clone and relocation specification creation (searching for a 
		## datastore cluster needs this information)
		relospec = vim.vm.RelocateSpec()
		relospec.pool = rpool
		clonespec = vim.vm.CloneSpec()
		clonespec.location = relospec
		clonespec.powerOn  = vm_poweron
		clonespec.template = False

		## VMware datastore
		datastore = None
		if vm_datastore:
			# Don't allow both datastore and datastore cluster to be provided
			if vm_datastore_cluster:
				raise RuntimeError("Only one of vm_datastore and vm_datastore_cluster should be set.")

			datastore = self.vmware_get_obj(content, [vim.Datastore], vm_datastore)

			if datastore is None:
				raise RuntimeError("Failed to locate destination datastore, '" + str(vm_datastore) + "'")

		## VMware datastore cluster
		if vm_datastore_cluster:
			# Don't need to check if vm_datastore is set here...

			# If we have a list of datastore clusters, re-order the list so that we don't
			# always choose the same one. We decide on where in the list to look based on 
			# the name of the VM
			if type(vm_datastore_cluster) is list:
				# Sum up the codepoints of the string to give us a number
				name_value = 0
				for c in vm_name:
					name_value += ord(c)
				name_value = name_value % len(vm_datastore_cluster)

				# Pick a number between 0 and the number of provided 
				# datastores clusters, and choose that datstore cluster
				ds_cluster_order = vm_datastore_cluster[name_value:] + vm_datastore_cluster[:name_value]
			else:
				ds_cluster_order = [vm_datastore_cluster]

			# We now have an array of possible clusters (either a single-element list or
			# a correctly ordered-list). Iterate over them one at a time until one of them
			# succeeds in giving us a recommendation.
			datastore = None
			for ds_cluster in ds_cluster_order:
				# Ask VMware to search for a volume within that cluster that is "recommended"

				# Get the datastore cluster and ensure it exists
				storage_pod = self.vmware_get_obj(content, [vim.StoragePod], ds_cluster)

				if storage_pod is None:
					raise RuntimeError("Failed to locate destination datastore cluster, '" + str(ds_cluster) + "' - check the workflow configuration and VMware to ensure the configured datastore cluster names match")

				# Build some selection specifications
				pod_spec = vim.storageDrs.PodSelectionSpec()
				pod_spec.storagePod = storage_pod
				storage_spec = vim.storageDrs.StoragePlacementSpec()
				storage_spec.type = vim.storageDrs.StoragePlacementSpec.PlacementType.clone
				storage_spec.folder = destfolder	# Not sure why it needs to know this, but it's required
				storage_spec.podSelectionSpec = pod_spec
				storage_spec.vm = template
				storage_spec.cloneSpec = clonespec
				storage_spec.cloneName = vm_name	# Not sure why it needs to know this, but it's required

				# Get VMware to make a recommendation
				result = content.storageResourceManager.RecommendDatastores(storage_spec)

				# If we don't get a result...
				if result is None:
					# Iterate to the next cluster
					continue

				# If we have a recommendation...
				if len(result.recommendations) > 0 and len(result.recommendations[0].action) and result.recommendations[0].action[0].destination is not None:
					# ...use the datastore and stop searching
					datastore = result.recommendations[0].action[0].destination
					break
				else:
					# Iterate to the next cluster
					continue

			# If VMware failed to return any recommendations on any cluster, then throw an error
			if datastore is None:
				raise RuntimeError("VMware did not return any storage recommendations on any of the configured storage clusters - check VMware and ensure there is capacity available within VMware's thresholds")

		## Populate relocation specification
		if datastore is not None:
			relospec.datastore = datastore

		## Finish off the clone spec
		clonespec.location = relospec
		if need_config_spec == True:
			clonespec.config = config_spec

		## If the user wants to customise the VM after creation...
		if not custspec == False:
			clonespec.customization = custspec

		return template.Clone(folder=destfolder, name=vm_name, spec=clonespec)

	############################################################################
	
	def update_vm_cache(self, vm, tag):
		"""Updates the VMware cache data with the information about the VM."""

		## Get a lock so that we're sure that the update vmware cache task (which updates
		## data for ALL vms) is not running. If it had been running our changes would be 
		## overwritten, so this way we wait for that to end before we try to update the cache.
		## The timeout is there in case this process dies or the server restarts or similar
		## and then the lock is never ever unlocked - the timeout ensures that eventually it is
		## and cortex might recover itself. maybe. the sleep is set to 1, up from the insane default of 0.1
		with self.rdb.lock('lock/update_vmware_cache',timeout=1800,sleep=1):

			# Get a cursor to the database
			cur = self.db.cursor(mysql.cursors.DictCursor)
		
			# Get the instance details of the vCenter given by tag
			instance = self.config['VMWARE'][tag]

			if vm.guest.hostName is not None:
				hostName = vm.guest.hostName
			else:
				hostName = ""

			if vm.guest.ipAddress is not None:
				ipAddress = vm.guest.ipAddress
			else:
				ipAddress = ""

			if vm.config.annotation is not None:
				annotation = vm.config.annotation
			else:
				annotation = ""

			# Put in the resource pool name rather than a Managed Object
			if vm.resourcePool is not None:
				cluster = vm.resourcePool.owner.name
			else:
				cluster = "None"

			# Put the VM in the database
			cur.execute("REPLACE INTO `vmware_cache_vm` (`id`, `vcenter`, `name`, `uuid`, `numCPU`, `memoryMB`, `powerState`, `guestFullName`, `guestId`, `hwVersion`, `hostname`, `ipaddr`, `annotation`, `cluster`, `toolsRunningStatus`, `toolsVersionStatus`) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", (vm._moId, instance['hostname'], vm.name, vm.config.uuid, vm.config.hardware.numCPU, vm.config.hardware.memoryMB, vm.runtime.powerState, vm.config.guestFullName, vm.config.guestId, vm.config.version, hostName, ipAddress, annotation, cluster, vm.guest.toolsRunningStatus, vm.guest.toolsVersionStatus2))

			# Commit
			self.db.commit()

	############################################################################
	
	def puppet_enc_register(self, system_id, certname, environment):
		"""
		"""

		# Get a cursor to the database
		curd = self.db.cursor(mysql.cursors.DictCursor)

		# Add environment if we've got it
		environments = dict((e['id'], e) for e in self.config['ENVIRONMENTS'] if e['puppet'])
		if environment is not None and environment in environments:
			puppet_environment = environments[environment]['puppet']

		# Insert the row
		curd.execute("INSERT INTO `puppet_nodes` (`id`, `certname`, `env`, `include_default`, `classes`, `variables`) VALUES (%s, %s, %s, %s, %s, %s)", (system_id, certname, puppet_environment, 1, "", ""))

		# Commit
		self.db.commit()

	############################################################################

	def vmware_vmreconfig_notes(self, vm, notes):
		"""Sets the notes annotation for the VM."""

		configSpec              = vim.VirtualMachineConfigSpec()
		configSpec.annotation   = notes
		return vm.ReconfigVM_Task(configSpec)

	############################################################################

	def vmware_vmreconfig_cpu(self, vm, cpus, cpus_per_socket, hotadd=True):
		"""Reconfigures the CPU count of a VM and enables/disabled CPU hot-add. 'vm' 
		is a PyVmomi managed object, 'cpus' is the TOTAL number of vCPUs to have, and
		'cpus_per_socket' is the number of cores per socket. The VM will end up having
		(cpus / cpus_per_socket) CPU sockets, each having cpus_per_socket. 'hotadd'
		is a boolean indicating whether or not to enable hot add or not.

		Returns a PyVmomi task object relating to the VMware task."""

		configSpec                   = vim.VirtualMachineConfigSpec()
		configSpec.cpuHotAddEnabled  = hotadd
		configSpec.numCPUs           = cpus
		configSpec.numCoresPerSocket = cpus_per_socket
		return vm.ReconfigVM_Task(configSpec)

	############################################################################

	def vmware_vmreconfig_ram(self, vm, megabytes=1024, hotadd=True):
		"""Reconfigures the amount of RAM a VM has, and whether memory can be hot-
		added to the system. 'vm' is a PyVmomi managed object, 'megabytes' is the 
		required amount of RAM in megabytes. 'hotadd' is a boolean indicating whether
		or not to enable hot add or not.

		Returns a PyVmomi task object relating to the VMware task."""

		configSpec                     = vim.VirtualMachineConfigSpec()
		configSpec.memoryHotAddEnabled = hotadd
		configSpec.memoryMB            = megabytes
		return vm.ReconfigVM_Task(configSpec)

	############################################################################

	def vmware_vm_add_disk(self, vm, size_in_bytes, thin=True):
		
		# find the last SCSI controller on the VM
		for device in vm.config.hardware.device:
			if isinstance(device, vim.vm.device.VirtualSCSIController):
				controller = device

			if hasattr(device.backing, 'fileName'):
				unit_number = int(device.unitNumber) + 1

				## unit_number 7 reserved scsi controller (cos of historical legacy stuff when LUN numbers were 0 to 7 and 7 was the highest priority hence the controller)
				if unit_number == 7:
					unit_number = 8

				## can't have more than 16 disks
				if unit_number >= 16:
					raise Exception("Too many disks on the SCSI controller")				

		if controller == None:
			raise Exception("No scsi controller found!")

		if unit_number == None:
			raise Exception("Unable to calculate logical unit number for SCSI device")

		newdev = vim.vm.device.VirtualDeviceSpec()		
		newdev.fileOperation = "create"
		newdev.operation = "add"
		newdev.device = vim.vm.device.VirtualDisk()

		newdev.device.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
		newdev.device.backing.diskMode = 'persistent'
		if thin:
			newdev.device.backing.thinProvisioned = True


		newdev.device.capacityInBytes = size_in_bytes
		newdev.device.capacityInKB = size_in_bytes * 1024
		newdev.device.controllerKey = controller.key
		newdev.device.unitNumber = unit_number

		devices = []
		devices.append(newdev)

		configSpec = vim.vm.ConfigSpec()
		configSpec.deviceChange = devices
		return vm.ReconfigVM_Task(spec=configSpec)

	############################################################################

	def vmware_vm_poweron(self, vm):
		"""Powers on a virtual machine."""

		return vm.PowerOn()

	############################################################################

	def vmware_vm_poweroff(self, vm):
		"""Powers off a virtual machine - does not do a guest shutdown (see vmware_vm_shutdown_guest)."""

		return vm.PowerOffVM_Task()

	############################################################################

	def vmware_wait_for_poweron(self, vm, timeout=30):
		"""Waits for a virtual machine to be marked as powered up by VMware."""

		# Initialise our timer
		timer = 0

		# While the VM is not marked as powered on, and our timer has not hit our timeout
		while vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOn and timer < timeout:
			# Wait
			time.sleep(1)
			timer = timer + 1

		# Return whether the VM is powered up or not
		return vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn

	############################################################################

	def vmware_vm_restart_guest(self, vm):
		"""Tells a virtual machine guest to restart."""

		return vm.RebootGuest()

	############################################################################

	def vmware_vm_shutdown_guest(self, vm):
		"""Tells a virtual machine guest to shutdown."""

		return vm.ShutdownGuest()

	############################################################################

	def vmware_collect_properties(self, service_instance, view_ref, obj_type, path_set=None, include_mors=False):
		"""
		Collect properties for managed objects from a view ref
		Check the vSphere API documentation for example on retrieving
		object properties:
			- http://goo.gl/erbFDz
		Args:
			si		(ServiceInstance): ServiceInstance connection
			view_ref (pyVmomi.vim.view.*): Starting point of inventory navigation
			obj_type	 (pyVmomi.vim.*): Type of managed object
			path_set			(list): List of properties to retrieve
			include_mors		 (bool): If True include the managed objects
									refs in the result
		Returns:
			A list of properties for the managed objects
		"""

		collector = service_instance.content.propertyCollector

		# Create object specification to define the starting point of
		# inventory navigation
		obj_spec = vmodl.query.PropertyCollector.ObjectSpec()
		obj_spec.obj = view_ref
		obj_spec.skip = True

		# Create a traversal specification to identify the path for collection
		traversal_spec = vmodl.query.PropertyCollector.TraversalSpec()
		traversal_spec.name = 'traverseEntities'
		traversal_spec.path = 'view'
		traversal_spec.skip = False
		traversal_spec.type = view_ref.__class__
		obj_spec.selectSet = [traversal_spec]

		# Identify the properties to the retrieved
		property_spec = vmodl.query.PropertyCollector.PropertySpec()
		property_spec.type = obj_type

		if not path_set:
			property_spec.all = True

		property_spec.pathSet = path_set

		# Add the object and property specification to the
		# property filter specification
		filter_spec = vmodl.query.PropertyCollector.FilterSpec()
		filter_spec.objectSet = [obj_spec]
		filter_spec.propSet = [property_spec]

		# Retrieve properties
		props = collector.RetrieveContents([filter_spec])

		data = []
		for obj in props:
			properties = {}
			for prop in obj.propSet:
				properties[prop.name] = prop.val

			if include_mors:
				properties['obj'] = obj.obj

			data.append(properties)

		return data

	############################################################################

	def vmware_set_guestinfo_variable(self, vm, variable, value):
		"""Sets a guestinfo variable that is accessible from VMware Tools
		inside the VM. Returns the VMware task."""

		configSpec = vim.VirtualMachineConfigSpec()
		configSpec.extraConfig.append(vim.OptionValue())
		configSpec.extraConfig[0].key = variable
		configSpec.extraConfig[0].value = value
		return vm.ReconfigVM_Task(configSpec)

	############################################################################

	def vmware_wait_for_customisations(self, service_instance, vm, desired_status=2, timeout=300):
		"""Waits for customisations"""

		# Build an event filter for the VM
		filterSpec = vim.EventFilterSpec()
		filterSpec.entity = vim.EventFilterSpecByEntity(entity=vm, recursion=vim.EventFilterSpecRecursionOption.self)

		# Initial status
		status = 0
		timer = 0

		# Whilst we're not at our desired status, and we've not timed out...
		while status != desired_status and timer < timeout:
			# Query latest events
			events = service_instance.content.eventManager.QueryEvents(filterSpec)

			# Iterate over the events
			for event in events:
				# If the event is the one we're looking for the break out
				if type(event) == vim.event.CustomizationStartedEvent and desired_status == 1:
					status = 1
					break
				elif type(event) == vim.event.CustomizationSucceeded and desired_status == 2:
					status = 2
					break
	
			# Sleep brifly if we've not hit our status
			if status != desired_status:
				time.sleep(1)
				timer = timer + 1

		# Return whether we reached the desired status or not (so return False on timeout)
		return status == desired_status

	############################################################################

	def vmware_get_container_view(self, service_instance, obj_type, container=None):
		"""
		Get a vSphere Container View reference to all objects of type 'obj_type'
		It is up to the caller to take care of destroying the View when no longer
		needed.
		Args:
		   obj_type (list): A list of managed object types
		Returns:
		   A container view ref to the discovered managed objects
		"""
		if not container:
			container = service_instance.content.rootFolder

		view_ref = service_instance.content.viewManager.CreateContainerView(container=container, type=obj_type, recursive=True)
		return view_ref

	############################################################################

	def vmware_get_objects(self, content, vimtype):
		"""
		Return an object by name, if name is None the
		first found object is returned
		"""
		obj = None
		container = content.viewManager.CreateContainerView(content.rootFolder, vimtype, True)
		return container.view

	############################################################################

	def set_link_ids(self, cortex_system_id, cmdb_id = None, vmware_uuid = None):
		"""
		Sets the identifiers that link a system from the Cortex `systems` 
		table to their relevant item in the CMDB.
		 - cortex_system_id: The id of the system in the table (not the name)
		 - cmdb_id: The value to store for the CMDB ID field.
                 - vmware_uuid: The UUID of the VM in VMware
		"""

		# Get a cursor to the database
		cur = self.db.cursor(mysql.cursors.DictCursor)

		# Update the database
		if cmdb_id is not None:
			cur.execute("UPDATE `systems` SET `cmdb_id` = %s WHERE `id` = %s", (cmdb_id, cortex_system_id))
		if vmware_uuid is not None:
			cur.execute("UPDATE `systems` SET `vmware_uuid` = %s WHERE `id` = %s", (vmware_uuid, cortex_system_id))

		# Commit
		self.db.commit()

	################################################################################

	def servicenow_link_task_to_ci(self, ci_sys_id, task_number):
		"""Links a ServiceNow 'task' (Incident Task, Project Task, etc.) to a CI so that it appears in the related records. 
		   Note that you should NOT use this function to link an incident to a CI (even though ServiceNow will kind of let
		   you do this...)
		     - ci_sys_id: The sys_id of the created configuration item, as returned by servicenow_create_ci
		     - task_number: The task number (e.g. INCTASK0123456, PRJTASK0123456) to link to. NOT the sys_id of the task."""

		# Request information about the task (incident task, project task, etc.) to get its sys_id
		r = requests.get('https://' + self.config['SN_HOST'] + '/api/now/v1/table/task?sysparm_fields=sys_id&sysparm_query=number=' + task_number, auth=(self.config['SN_USER'], self.config['SN_PASS']), headers={'Accept': 'application/json'})

		# Check we got a valid response
		if r is not None and r.status_code >= 200 and r.status_code <= 299:
			# Get the response
			response_json = r.json()
			
			# This returns something like this:
			# {"result": [{"sys_id": "f49bc4bb0fc3b500488ec453e2050ec3", "number": "PRJTASK0123456"}]}
			
			# Get the sys_id of the task
			try:
				task_sys_id = response_json['result'][0]['sys_id']
			except Exception as e:
				# Handle JSON not containing 'result', a first element, or a 'sys_id' parameter (not that this should happen, really...)
				raise Exception("Failed to query ServiceNow for task information. Invalid response from ServiceNow.")

			# Make a post request to link the given CI to the task
			r = requests.post('https://' + self.config['SN_HOST'] + '/api/now/v1/table/task_ci', auth=(self.config['SN_USER'], self.config['SN_PASS']), headers={'Accept': 'application/json', 'Content-Type': 'application/json'}, json={'ci_item': ci_sys_id, 'task': task_sys_id})

			# If we succeeded, return the sys_id of the link table entry
			if r is not None and r.status_code >= 200 and r.status_code <= 299:
				return response_json['result'][0]['sys_id']
			else:
				error = "Failed to link ServiceNow task and CI."
				if r is not None:
					error = error + " HTTP Response code: " + str(r.status_code)
				raise Exception(error)
		else:
			# Return error with appropriate information
			error = "Failed to query ServiceNow for task information."
			if r is not None:
				if r.status_code == 404:
					error = error + " Task '" + str(task_number) + "' does not exist"
				else:
					error = error + " HTTP Response code: " + str(r.status_code)
			raise Exception(error)


	################################################################################

	def servicenow_create_ci(self, ci_name, os_type, os_name, sockets='', cores_per_socket='', ram_mb='', disk_gb='', ipaddr='', virtual=True, environment=None, short_description='', comments='', location=None):
		"""Creates a new CI within ServiceNow.
		     - ci_name: The name of the CI, e.g. srv01234
		     - os_type: The OS type as a number, see OS_TYPE_BY_NAME
		     - os_name: The name of the OS, as used by ServiceNow
		     - cpus: The total number of CPUs the CI has
		     - ram_mb: The amount of RAM of the CI in MeB
		     - disk_gb: The total amount of disk space in GiB
		     - ipaddr: The IP address of the CI
		     - virtual: Boolean indicating if the CI is a VM (True) or Physical (False). Defaults to True.
		     - environment: The id of the environment (see the application configuration) that the CI is in
		     - short_description: The value of the short_description (Description) field in ServiceNow. A purpose, or something.
		     - comments: The value of the comments field in ServiceNow. Any random information.
		     - location: The value of the location field in ServiceNow

		On success, returns the sys_id of the object created in ServiceNow.
		"""

		# Decide which ServiceNow table we need to put the CI in to, based on the OS
		if os_type == self.OS_TYPE_BY_NAME['Linux']:
			table_name = "cmdb_ci_linux_server"
			model_id = "Generic Linux Virtual Server"
		elif os_type == self.OS_TYPE_BY_NAME['Windows']:
			table_name = "cmdb_ci_win_server"
			model_id = "Generic Windows Virtual Server"
		elif os_type == self.OS_TYPE_BY_NAME['ESXi']:
			table_name = "cmdb_ci_esx_server"
			model_id = ""
		elif os_type == self.OS_TYPE_BY_NAME['Solaris']:
			table_name = "cmdb_ci_solaris_server"
			model_id = "Generic UNIX Virtual Server"
		else:
			raise Exception('Unknown os_type passed to servicenow_create_ci')

		# Build the data for the CI
		vm_data = { 'name': str(ci_name), 'os': str(os_name), 'virtual': str(virtual).lower(), 'ip_address': ipaddr, 'operational_status': 'In Service', 'model_id': model_id, 'short_description': short_description, 'comments': comments }

		# Add CPU count if we've got it
		if sockets is not None:
			vm_data['cpu_count'] = str(sockets)
		if cores_per_socket is not None:
			vm_data['cpu_core_count'] = str(cores_per_socket)

		# Add disk space if we've got it
		if disk_gb is not None:
			vm_data['disk_space'] = str(disk_gb)

		# Add RAM if we've got it
		if ram_mb is not None:
			vm_data['ram'] = str(ram_mb)

		# Add location if we've got it
		if location is not None:
			vm_data['location'] = location

		# Add environment if we've got it
		environments = dict((e['id'], e) for e in self.config['ENVIRONMENTS'] if e['cmdb'])
		if environment is not None and environment in environments:
			vm_data['u_environment'] = environments[environment]['cmdb']

		# json= was only added in Requests 2.4.2, so might need to be data=json.dumps(vm_data)
		# Content-Type header may be superfluous as Requests might add it anyway, due to json=
		r = requests.post('https://' + self.config['SN_HOST'] + '/api/now/v1/table/' + table_name + "?sysparm_display_value=true", auth=(self.config['SN_USER'], self.config['SN_PASS']), headers={'Accept': 'application/json', 'Content-Type': 'application/json'}, json=vm_data)

		# Parse the response
		if r is not None and r.status_code >= 200 and r.status_code <= 299:
			# Parse the JSON and return the sys_id that ServiceNow gives us along with the CMDB ID (u_number)
			response_json = r.json()
			retval = (response_json['result']['sys_id'], response_json['result']['u_number'])
		else:
			error = "Failed to create ServiceNow object. API Request failed."
			if r is not None:
				error = error + " HTTP Response code: " + str(r.status_code)
			raise Exception(error)

		# Get a cursor to the database
		curd = self.db.cursor(mysql.cursors.DictCursor)

		# Update the cache row
		curd.execute("REPLACE INTO `sncache_cmdb_ci` (`sys_id`, `sys_class_name`, `name`, `operational_status`, `u_number`, `short_description`, `u_environment`, `virtual`) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (retval[0], response_json['result']['sys_class_name'], vm_data['name'], 'In Service', retval[1], short_description, response_json['result']['u_environment'], 1))
		self.db.commit()

		return retval

	################################################################################
	
	def _connect_redis(self):
		"""Connects to the Redis instance specified in the configuration and 
		returns a StrictRedis object"""

		return redis.StrictRedis(host=self.config['REDIS_HOST'], port=self.config['REDIS_PORT'], db=0)

	################################################################################

	def redis_set_vm_data(self, vm, key, value, expire=28800):
		"""Sets a value in Redis relating to a VM."""

		if expire is not None:
			self.rdb.setex("vm/" + vm.config.uuid + "/" + key, expire, value)
		else:
			self.rdb.set("vm/" + vm.config.uuid + "/" + key, value)

	################################################################################

	def redis_get_vm_data(self, vm, key):
		"""Gets a value in Redis relating to a VM."""

		return self.rdb.get("vm/" + vm.config.uuid + "/" + key)

	################################################################################

	def wait_for_guest_notify(self, vm, states, timeout=28800):
		"""Waits for the in-guest customisations to become one of the listed states."""

		# Get the current state of the notify variable
		notify = self.redis_get_vm_data(vm, 'notify')

		# Start a timer
		timer = 0

		# Whilst we've not hit our timeout, and the installer hasn't set a
		# notification value that is one of our states...
		while timer < timeout:
			# If we have a value for notify and the value is one of
			# the states we are looking for...
			if notify is not None and notify in states:
				# ...escape the loop
				break

			# ...sleep and increment timer
			time.sleep(1)
			timer = timer + 1

			# Get the lastest value of the notify
			notify = self.redis_get_vm_data(vm, 'notify')

		# Return the latest value, which may be None if the in-guest installer
		# never runs. Otherwise it can be any other value, which may not
		# necessarily be one of the given states.
		return notify

	########################################################################

	def send_email(self, to_addr, subject, contents):
		# Get e-mail configuration
		server = self.config['SMTP_SERVER']
		from_addr = self.config['EMAIL_FROM']

		# Add on a default domain for recipient if we don't have one
		if to_addr.find('@') < 0 and 'EMAIL_DOMAIN' in self.config and len(self.config['EMAIL_DOMAIN']) > 0:
			to_addr = to_addr + '@' + self.config['EMAIL_DOMAIN']

		# Build the message
		msg = MIMEText(contents)
		msg['Subject'] = subject
		msg['From'] = from_addr
		msg['To'] = to_addr

		# Send the mail
		smtp = smtplib.SMTP(server)
		smtp.sendmail(from_addr, [to_addr], msg.as_string())
		smtp.quit()

	########################################################################

	def _get_winrpc_proxy(self, environment):
		"""Connects to the Cortex Windows RPC proxy.
		Args:
		  environment (string): The environment key name to get the details from the config
		Returns:
		  A Pyro4.Proxy object connected to the appropriate environment"""

		# Set up a proxy
		proxy = Pyro4.Proxy('PYRO:CortexWindowsRPC@' + self.config['WINRPC'][environment]['host'] + ':' + str(self.config['WINRPC'][environment]['port']))
		proxy._pyroHmacKey = self.config['WINRPC'][environment]['key']

		# Attempt to ping the proxy
		proxy.ping()

		return proxy

	########################################################################

	def windows_move_computer_to_default_ou(self, hostname, environment):
		"""Moves a Computer object in Active Directory to reside within the default OU.
		Args:
		  hostname (string): The hostname of the computer object to move
		Returns:
		  Nothing."""

		if self._get_winrpc_proxy(environment).move_to_default_ou(hostname) != 0:
			raise Exception('Remote call returned failure response - check the Cortex Windows RPC log file')

		# Performing other Windows tasks too quickly can result in them
		# silently failing, so give AD some time to catch up
		time.sleep(5)

	########################################################################

	def windows_join_groups(self, hostname, environment, groups):
		"""Adds a Computer object in Active Directory to the default list of groups.
		Args:
		  hostname (string): The hostname of the computer object to put in to groups
		  environment (string): The key-name of the environment (e.g. prod, dev)
		  groups (list): The list of groups to join. Just names, not DNs.
		Returns:
		  Nothing."""

		if self._get_winrpc_proxy(environment).join_groups(hostname, groups) != 0:
			raise Exception('Remote call returned failure response - check the Cortex Windows RPC log file')

		# Performing other Windows tasks too quickly can result in them
		# silently failing, so give AD some time to catch up
		time.sleep(5)

	########################################################################

	def windows_set_computer_details(self, hostname, environment, description, location):
		"""Sets various details about the computer object in AD.
		Args:
		  hostname (string): The hostname of the computer object to modify
                  description (string): The description of the computer
		  location (string): The location of the computer
		Returns:
		  Nothing."""

		if self._get_winrpc_proxy(environment).set_information(hostname, description, location) != 0:
			raise Exception('Remote call returned failure response - check the Cortex Windows RPC log file')

		# Performing other Windows tasks too quickly can result in them
		# silently failing, so give AD some time to catch up
		time.sleep(5)

	################################################################################

	## Return a boolean indicating whether a computer object exists in AD in the
	## specified environment with the given hostname
	def windows_computer_object_exists(self, env, hostname):
		return self._get_winrpc_proxy(env).find_computer_object(hostname)

	################################################################################

	## Deletes a computer object exists in AD in the specified environment with the 
	## given hostname
	def windows_delete_computer_object(self, env, hostname):
		return self._get_winrpc_proxy(env).delete_computer_object(hostname)

	#######################################################################

	def vmware_get_vm_by_uuid(self, uuid, vcenter):
		"""Get a VM by UUID
		Args:
		  uuid (string): UUID to find
		  vcenter (string): vcenter to search in
		Returns:
		  VM if found or None otherwise"""

		# Connect to the correct vCenter
		instance = None
		for key in self.config['VMWARE']:
		        if self.config['VMWARE'][key]['hostname'] == vcenter: 
		                instance = key
		
		# If we've found the right vCenter
		if instance is not None:
			# Connect
			si = self.vmware_smartconnect(instance)
			content = si.RetrieveContent()
			# Search for the VM by UUID
			return content.searchIndex.FindByUuid(None, uuid, True, False)
		else:
			## TODO shouldn't this raise an exception?!
			return None