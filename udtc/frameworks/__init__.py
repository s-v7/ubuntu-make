# -*- coding: utf-8 -*-
# Copyright (C) 2014 Canonical
#
# Authors:
#  Didier Roche
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; version 3.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA


"""Base Handling functions and base class of backends"""

import abc
from contextlib import suppress
from importlib import import_module, reload
import inspect
import logging
import os
import pkgutil
import sys
import subprocess
from udtc.network.requirements_handler import RequirementsHandler
from udtc.settings import DEFAULT_INSTALL_TOOLS_PATH
from udtc.tools import ConfigHandler, NoneDict, classproperty, get_current_arch, get_current_ubuntu_version,\
    is_completion_mode, switch_to_current_user, MainLoop
from udtc.ui import UI


logger = logging.getLogger(__name__)


class BaseCategory():
    """Base Category class to be inherited"""

    NOT_INSTALLED, PARTIALLY_INSTALLED, FULLY_INSTALLED = range(3)
    categories = NoneDict()

    def __init__(self, name, description="", logo_path=None, is_main_category=False, packages_requirements=None):
        self.name = name
        self.description = description
        self.logo_path = logo_path
        self.is_main_category = is_main_category
        self.default = None
        self.frameworks = NoneDict()
        self.packages_requirements = [] if packages_requirements is None else packages_requirements
        if self.prog_name in self.categories:
            logger.error("There is already a registered category with {} as a name. Don't register the second one."
                         .format(name))
        else:
            self.categories[self.prog_name] = self

    @classproperty
    def main_category(self):
        for category in self.categories.values():
            if category.is_main_category:
                return category
        return None

    @property
    def prog_name(self):
        """Get programmatic, path and CLI compatible names"""
        return self.name.lower().replace('/', '-').replace(' ', '-')

    @property
    def default_framework(self):
        """Get default framework"""
        for framework in self.frameworks.values():
            if framework.is_category_default:
                return framework
        return None

    def register_framework(self, framework):
        """Register a new framework"""
        if framework.prog_name in self.frameworks:
            logger.error("There is already a registered framework with {} as a name. Don't register the second one."
                         .format(framework.name))
        else:
            self.frameworks[framework.prog_name] = framework

    @property
    def is_installed(self):
        """Return if the category is installed"""
        installed_frameworks = [framework for framework in self.frameworks.values() if framework.is_installed]
        if len(installed_frameworks) == 0:
            return self.NOT_INSTALLED
        if len(installed_frameworks) == len(self.frameworks):
            return self.FULLY_INSTALLED
        return self.PARTIALLY_INSTALLED

    def install_category_parser(self, parser):
        """Install category parser and get frameworks"""
        if not self.has_frameworks():
            logging.debug("Skipping {} having no framework".format(self.name))
            return
        # framework parser is directly category parser
        if self.is_main_category:
            framework_parser = parser
        else:
            category_parser = parser.add_parser(self.prog_name, help=self.description)
            framework_parser = category_parser.add_subparsers(dest="framework")
        for framework in self.frameworks.values():
            framework.install_framework_parser(framework_parser)
        return framework_parser

    def has_frameworks(self):
        """Return if a category has at least one framework"""
        return len(self.frameworks) > 0

    def has_one_framework(self):
        """Return if a category has one framework"""
        return len(self.frameworks) == 1

    def run_for(self, args):
        """Running commands from args namespace"""
        # try to call default framework if any
        if not args.framework:
            if not self.default_framework:
                message = "A default framework for category {} was requested where there is none".format(self.name)
                logger.error(message)
                raise BaseException(message)
            self.default_framework.run_for(args)
            return
        self.frameworks[args.framework].run_for(args)


class BaseFramework(metaclass=abc.ABCMeta):

    def __init__(self, name, description, category, logo_path=None, is_category_default=False, install_path_dir=None,
                 only_on_archs=None, only_ubuntu_version=None, packages_requirements=None):
        self.name = name
        self.description = description
        self.logo_path = None
        self.category = category
        self.is_category_default = is_category_default
        self.only_on_archs = [] if only_on_archs is None else only_on_archs
        self.only_ubuntu_version = [] if only_ubuntu_version is None else only_ubuntu_version
        self.packages_requirements = [] if packages_requirements is None else packages_requirements
        self.packages_requirements.extend(self.category.packages_requirements)

        # don't detect anything for completion mode (as we need to be quick), so avoid opening apt cache and detect
        # if it's installed.
        if is_completion_mode():
            category.register_framework(self)
            return

        self.need_root_access = False
        with suppress(KeyError):
            self.need_root_access = not RequirementsHandler().is_bucket_installed(self.packages_requirements)

        if self.is_category_default:
            if self.category == BaseCategory.main_category:
                logger.error("Main category can't have default framework as {} requires".format(name))
                self.is_category_default = False
            elif self.category.default_framework is not None:
                logger.error("Can't set {} as default for {}: this category already has a default framework ({}). "
                             "Don't set any as default".format(category.name, name,
                                                               self.category.default_framework.name))
                self.is_category_default = False
                self.category.default_framework.is_category_default = False

        if not install_path_dir:
            install_path_dir = os.path.join("" if category.is_main_category else category.prog_name, self.prog_name)
        self.default_install_path = os.path.join(DEFAULT_INSTALL_TOOLS_PATH, install_path_dir)
        self.install_path = self.default_install_path
        # check if we have an install path previously set
        config = ConfigHandler().config
        try:
            self.install_path = config["frameworks"][category.prog_name][self.prog_name]["path"]
        except (TypeError, KeyError, FileNotFoundError):
            pass

        # This requires install_path and will register need_root or not
        if not self.is_installed and not self.is_installable:
            logger.info("Don't register {} as it's not installable on this configuration.".format(name))
            return

        category.register_framework(self)

    @property
    def is_installable(self):
        """Return if the framework can be installed on that arch"""
        try:
            if len(self.only_on_archs) > 0:
                # we have some restricted archs, check we support it
                current_arch = get_current_arch()
                if current_arch not in self.only_on_archs:
                    logger.debug("{} only supports {} archs and you are on {}.".format(self.name, self.only_on_archs,
                                                                                       current_arch))
                    return False
            if len(self.only_ubuntu_version) > 0:
                current_version = get_current_ubuntu_version()
                if current_version not in self.only_ubuntu_version:
                    logger.debug("{} only supports {} and you are on {}.".format(self.name, self.only_ubuntu_version,
                                                                                 current_version))
                    return False
            if not RequirementsHandler().is_bucket_available(self.packages_requirements):
                return False
        except:
            logger.error("An error occurred when detecting platform, don't register {}".format(self.name))
            return False
        return True

    @property
    def prog_name(self):
        """Get programmatic, path and CLI compatible names"""
        return self.name.lower().replace('/', '-').replace(' ', '-')

    @abc.abstractmethod
    def setup(self, install_path=None):
        """Method call to setup the Framework"""
        if not self.is_installable:
            logger.error("You can't install that framework on that machine")
            UI.return_main_screen()
            return

        if self.need_root_access and os.geteuid() != 0:
            logger.debug("Requesting root access")
            cmd = ["sudo", "-E", "env", "PATH={}".format(os.getenv("PATH"))]
            cmd.extend(sys.argv)
            MainLoop().quit(subprocess.call(cmd))

        # be a normal, kind user
        switch_to_current_user()

        if install_path:
            self.install_path = install_path

    def mark_in_config(self):
        """Mark the installation as installed in the config file"""
        config = ConfigHandler().config
        config.setdefault("frameworks", {})\
              .setdefault(self.category.prog_name, {})\
              .setdefault(self.prog_name, {})["path"] = self.install_path
        ConfigHandler().config = config

    @property
    def is_installed(self):
        """Method call to know if the framework is installed"""
        if not os.path.isdir(self.install_path):
            return False
        if not RequirementsHandler().is_bucket_installed(self.packages_requirements):
            return False
        logger.debug("{} is installed".format(self.name))
        return True

    def install_framework_parser(self, parser):
        """Install framework parser"""
        this_framework_parser = parser.add_parser(self.prog_name, help=self.description)
        this_framework_parser.add_argument('destdir', nargs='?')
        return this_framework_parser

    def run_for(self, args):
        """Running commands from args namespace"""
        logger.debug("Call run_for on {}".format(self.name))
        self.setup(args.destdir)


class MainCategory(BaseCategory):

    def __init__(self):
        super().__init__(name="main", is_main_category=True)


def _is_categoryclass(o):
    return inspect.isclass(o) and issubclass(o, BaseCategory)


def _is_frameworkclass(o):
    return inspect.isclass(o) and issubclass(o, BaseFramework)


def load_frameworks():
    """Load all modules and assign to correct category"""
    main_category = MainCategory()
    for loader, module_name, ispkg in pkgutil.iter_modules(path=[os.path.dirname(__file__)]):
        module_name = "{}.{}".format(__package__, module_name)
        logger.debug("New framework module: {}".format(module_name))
        if module_name not in sys.modules:
            import_module(module_name)
        else:
            reload(sys.modules[module_name])
        module = sys.modules[module_name]
        current_category = main_category  # if no category found -> we assign to main category
        for category_name, CategoryClass in inspect.getmembers(module, _is_categoryclass):
            logger.debug("Found category: {}".format(category_name))
            current_category = CategoryClass()
        for framework_name, FrameworkClass in inspect.getmembers(module, _is_frameworkclass):
            try:
                if FrameworkClass(current_category) is not None:
                    logger.debug("Attach framework {} to {}".format(framework_name, current_category.name))
            except TypeError as e:
                logger.error("Can't attach {} to {}: {}".format(framework_name, current_category.name, e))