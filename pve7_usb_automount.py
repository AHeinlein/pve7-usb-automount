#!/usr/bin/python3
# -*- coding: utf-8 -*-

import sys
import re
import signal
import pyudev
import time
import configparser
import syslog
import subprocess
import json

from os.path import exists
from pyudev.version import __version_info__

from PySide2.QtCore import *

config = configparser.ConfigParser()
config.read('/etc/pve-usb-automount/main.conf')

MAX_FILES = config.get("MAIN", "MAX_FILES", fallback=3)
MOUNT_OPTS = config.get("MAIN", "MOUNT_OPTS", fallback="sync")
USE_LABEL = config.get("MAIN", "USE_LABEL", fallback=0)

signal.signal(signal.SIGINT, signal.SIG_DFL)
syslog.openlog(ident="pve7-usb-automount")

class USBAutomount:
    def __init__(self):
        self.app = QCoreApplication(sys.argv)

        print("Starting PVE7-USB-Automount")

        # Starte UDEV-observer Thread
        self.udev_thread = QThread()
        self.udev_worker = udevObserver()
        self.udev_worker.moveToThread(self.udev_thread)
        self.udev_thread.started.connect(self.udev_worker.process)
        self.udev_worker.finished.connect(self.udev_thread.quit)
        self.udev_thread.start()

        sys.exit(self.app.exec_())

class udevObserver(QObject):
    finished = Signal()
    keeprunning = True

    def process(self):
        print("Starting thread UDEV-Observer")

        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by('block')

        observer = pyudev.MonitorObserver(monitor, self.udev_event)
        observer.start()

        while self.keeprunning:
            time.sleep(0.3)

        observer.stop()
        self.finished.emit()
        print("Exiting observer finished")

    def quit(self):
        print("Exiting observer")
        self.keeprunning = False

    def udev_event(self, action, device):
        syslog.syslog("UDEV event for action::" + action + " and device::" + device.sys_name + " device_path::" + device.device_path)
        devattr = device.attributes.available_attributes if __version_info__[1] >= 21 else device.attributes

        if action == "add":
            syslog.syslog("Add event for " + device.sys_name)

            usbdev = device.find_parent('usb')
            if usbdev == None:
                syslog.syslog("This is no USB device %s, we dont use it" % device.sys_name)
                return

            if device.device_type in ["disk", "partition"] and device.subsystem in ["block"]:
                syslog.syslog("Found USB device %s, try to mount" % device.sys_name)
                devinfo = self.getDeviceInfo(device.sys_name)
                if devinfo != -1:
                    self.mountDevice(devinfo)
                else:
                    syslog.syslog("No device info found for %s" % device.sys_name)

        if action == "remove":
            syslog.syslog("Remove event for " + device.sys_name)

            if device.device_type in ["disk", "partition"] and device.subsystem in ["block"]:
                self.umountDevice(device.sys_name)

    def getDeviceInfo(self, device):
        p = subprocess.Popen("lsblk -J -d -o KNAME,LABEL,FSTYPE /dev/%s" % device, stdout=subprocess.PIPE, shell=True)
        p.wait(timeout=10)
        if p.returncode == 0:
            rawout = p.stdout.read().decode('UTF-8')
            out = json.loads(rawout)
            return out['blockdevices'][0]
        else:
            return -1

    def mountDevice(self, device):
        if device["fstype"] == None or device["fstype"] == "iso9660":
            syslog.syslog("Blockdevice has no fs-type")
            return

        if "label" in device and device["label"] != None:
            device["label"] = device["label"].replace(" ", "_")
        
        if USE_LABEL:
            name = device["label"] if "label" in device and device["label"] != None else device["kname"]
        else:
            name = device["kname"]
        mountpath = "/media/%s" % name
        syslog.syslog("Create mount dir <%s>" % mountpath)
        if not exists(mountpath):
            p = subprocess.Popen("mkdir -p '%s'" % mountpath, stdout=subprocess.PIPE, shell=True)
            p.wait(timeout=10)
            if p.returncode != 0:
                syslog.syslog("Mount dir could not be created")
                return

        p = subprocess.Popen("mount -o %s /dev/%s '%s'" % (MOUNT_OPTS, device["kname"], mountpath), stdout=subprocess.PIPE, shell=True)
        p.wait(timeout=10)
        if p.returncode == 0:
            syslog.syslog("Device %s was mounted on %s" % (device["kname"], mountpath))
            subprocess.Popen("pvesm add dir 'usb-%s' -path '%s' -maxfiles %s -content vztmpl,iso,backup -is_mountpoint 1" % (name, mountpath, MAX_FILES), stdout=subprocess.PIPE, shell=True)
        else:
            syslog.syslog("Device %s was NOT mounted %s" % (device["kname"], mountpath))

    def umountDevice(self, device):
        mountpath = self.getMountPathForDevice(device)
        if mountpath == "":
            syslog.syslog("Mount dir for device %s was not found" % device)
            return

        p = subprocess.Popen("umount '%s'" % mountpath, stdout=subprocess.PIPE, shell=True)
        p.wait(timeout=10)
        if p.returncode == 0:
            syslog.syslog("Device %s was unmounted" % (device))
            subprocess.Popen("pvesm remove 'usb-%s'" % (device), stdout=subprocess.PIPE, shell=True)
            if exists(mountpath):
                subprocess.Popen("rmdir '%s'" % mountpath, stdout=subprocess.PIPE, shell=True)
        else:
            syslog.syslog("Device %s was NOT unmounted" % (device))

    def getMountPathForDevice(self, device):
        p = subprocess.Popen("mount", stdout=subprocess.PIPE, shell=True)
        p.wait(timeout=10)
        if p.returncode == 0:
            rawout = p.stdout.read().decode('UTF-8')
            for line in rawout.split("\n"):
                m = re.search("^/dev/%s on (.*?) type" % device, line)
                if m != None:
                    return m.group(1)
        else:
            return ""

        return ""

if __name__ == '__main__':
    USBAutomount()
