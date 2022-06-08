# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2022 NV Access Limited
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.
import typing
import enum
from enum import (
	Enum,
)
from typing import (
	List,
	Optional,
	Dict,
)
import threading

import addonHandler
import core
import extensionPoints
from addonHandler import addonVersionCheck
from utils.displayString import DisplayStringEnum
from addonStore.dataManager import DataManager
from logHandler import log

from addonStore.models import (
	AddonDetailsModel,
)

if typing.TYPE_CHECKING:
	# Remove when https://github.com/python/typing/issues/760 is resolved
	from _typeshed import SupportsLessThan


@enum.unique
class AvailableAddonStatus(DisplayStringEnum):
	""" Values to represent the status of add-ons within the NVDA add-on store.
	Although related, these are independent of the states in L{addonHandler}
	"""
	# noinspection PyArgumentList
	AVAILABLE = enum.auto()
	# noinspection PyArgumentList
	DOWNLOADING = enum.auto()
	# noinspection PyArgumentList
	DOWNLOAD_FAILED = enum.auto()
	# noinspection PyArgumentList
	INSTALLING = enum.auto()
	# noinspection PyArgumentList
	INSTALL_FAILED = enum.auto()
	# noinspection PyArgumentList
	INSTALLED = enum.auto()

	@property
	def _displayStringLabels(self) -> Dict[Enum, str]:
		_labels = {
			AvailableAddonStatus.AVAILABLE: _("Available"),
			AvailableAddonStatus.DOWNLOADING: _("Downloading"),
			AvailableAddonStatus.DOWNLOAD_FAILED: _("Download failed"),
			AvailableAddonStatus.INSTALLING: _("Installing"),
			AvailableAddonStatus.INSTALL_FAILED: _("Install failed"),
			AvailableAddonStatus.INSTALLED: _("Installed, restart required"),
		}
		return _labels


class AddonListItemVM:
	def __init__(
			self,
			model: AddonDetailsModel,
			status: AvailableAddonStatus = AvailableAddonStatus.AVAILABLE
	):
		self._model: AddonDetailsModel = model  # read-only
		self._status: AvailableAddonStatus = status  # modifications triggers L{updated.notify}
		self.updated = extensionPoints.Action()  # Notify of changes to VM, argument: addonListItemVM

	@property
	def model(self):
		return self._model

	@property
	def status(self):
		return self._status

	@status.setter
	def status(self, newStatus: AvailableAddonStatus):
		if newStatus != self.status:
			log.debug(f"addon status change: {self.Id}: status: {newStatus}")
			self._status = newStatus
			# ensure calling on the main thread.
			core.callLater(delay=0, callable=self.updated.notify, addonListItemVM=self)

	@property
	def Id(self) -> Optional[str]:
		return self._model.addonId


class AddonDetailsVM:
	def __init__(self, listItem: Optional[AddonListItemVM] = None):
		self._listItem: Optional[AddonListItemVM] = listItem
		self.updated = extensionPoints.Action()  # triggered by setting L{self._listItem}

	@property
	def listItem(self) -> Optional[AddonListItemVM]:
		return self._listItem

	@listItem.setter
	def listItem(self, newListItem: Optional[AddonListItemVM]):
		if (
			self._listItem == newListItem  # both may be same ref or None
			or (
				None not in (newListItem, self._listItem)
				and self._listItem.Id == newListItem.Id  # confirm with addonId
			)
		):
			# already set, exit early
			return
		self._listItem = newListItem
		# ensure calling on the main thread.
		core.callLater(delay=0, callable=self.updated.notify, addonDetailsVM=self)


class AddonListVM:
	presentedAttributes = [
		"displayName",
		"versionName",
		"publisher",
		"status",  # NVDA state for this addon, see L{AvailableAddonStatus}
	]

	def __init__(
			self,
			addons: List[AddonListItemVM],
	):
		self._addons: Dict[str, AddonListItemVM] = {}
		self.itemUpdated = extensionPoints.Action()
		self.updated = extensionPoints.Action()
		self.selectionChanged = extensionPoints.Action()
		self.selectedAddonId: Optional[str] = None
		self.lastSelectedAddonId = self.selectedAddonId
		self._sortByModelFieldName: str = "displayName"
		self._filterString: typing.Optional[str] = None

		self._setSelectionPending = False
		self._addonsFilteredOrdered: typing.List[str] = self._getFilteredSortedIds()
		self._validate(
			sortField=self._sortByModelFieldName,
			selectionIndex=self.getSelectedIndex(),
			selectionId=self.selectedAddonId
		)
		self.selectedAddonId = self._tryPersistSelection(self._addonsFilteredOrdered)
		self.resetListItems(addons)

	def _itemDataUpdated(self, addonListItemVM: AddonListItemVM):
		addonId: str = addonListItemVM.Id
		log.debug(f"status: {addonListItemVM.status}")
		log.debug(f"equal instances: {addonListItemVM == self._addons[addonId]}")
		if addonId in self._addonsFilteredOrdered:
			log.debug("calling")
			index = self._addonsFilteredOrdered.index(addonId)
			# ensure calling on the main thread.
			core.callLater(delay=0, callable=self.itemUpdated.notify, index=index)

	def resetListItems(self, listVMs: List[AddonListItemVM]):
		log.debug("resetting list items")

		# Ensure that old listItemVMs can no longer notify of updates.
		for _addonListItemVM in self._addons.values():
			_addonListItemVM.updated.unregister(self._itemDataUpdated)

		# set new ID:listItemVM mapping.
		self._addons: Dict[str, AddonListItemVM] = {
			vm.Id: vm
			for vm in listVMs
		}
		self._updateAddonListing()

		# allow new listItemVMs to notify of updates.
		for _addonListItemVM in listVMs:
			_addonListItemVM.updated.register(self._itemDataUpdated)

		# Notify observers of change in the list.
		# ensure calling on the main thread.
		core.callLater(delay=0, callable=self.updated.notify)

	def getAddonAttrText(self, index: int, attrName: str) -> str:
		""" Get the text for an item's attribute.
		@param index: The index of the item in _addonsFilteredOrdered
		@param attrName: The exposed attribute for the addon. See L{AddonList.presentedAttributes}
		@return: The text for the addon attribute
		"""
		addonId = self._addonsFilteredOrdered[index]
		listItemVM = self._addons[addonId]
		return self._getAddonAttrText(listItemVM, attrName)

	def _getAddonAttrText(self, listItemVM: AddonListItemVM, attrName: str) -> str:
		assert attrName in AddonListVM.presentedAttributes
		if attrName == "status":  # special handling, not on the model.
			return listItemVM.status.displayString
		return getattr(listItemVM.model, attrName)

	def getCount(self) -> int:
		return len(self._addonsFilteredOrdered)

	def getSelectedIndex(self) -> Optional[int]:
		if self._addonsFilteredOrdered and self.selectedAddonId in self._addonsFilteredOrdered:
			return self._addonsFilteredOrdered.index(self.selectedAddonId)
		return None

	def setSelection(self, index: Optional[int]) -> Optional[AddonListItemVM]:
		self._validate(selectionIndex=index)
		self.selectedAddonId = self._addonsFilteredOrdered[index] if index is not None else None
		selectedItemVM: Optional[AddonListItemVM] = self.getSelection()
		log.debug(f"selected Item: {selectedItemVM}")
		# ensure calling on the main thread.
		core.callLater(delay=0, callable=self.selectionChanged.notify)
		return selectedItemVM

	def getSelection(self) -> Optional[AddonListItemVM]:
		return self._addons.get(self.selectedAddonId, None)

	def _validate(
			self,
			sortField: Optional[str] = None,
			selectionIndex: Optional[int] = None,
			selectionId: Optional[str] = None,
	):
		if sortField is not None:
			assert (sortField in AddonListVM.presentedAttributes)
		if selectionIndex is not None:
			assert (0 <= selectionIndex < len(self._addonsFilteredOrdered))
		if selectionId is not None:
			assert (selectionId in self._addons.keys())

	def setSortField(self, modelFieldName: str):
		oldOrder = self._addonsFilteredOrdered
		self._validate(sortField=modelFieldName)
		self._sortByModelFieldName = modelFieldName
		self._updateAddonListing()
		if oldOrder != self._addonsFilteredOrdered:
			# ensure calling on the main thread.
			core.callLater(delay=0, callable=self.updated.notify)

	def _getFilteredSortedIds(self) -> List[str]:
		def _getSortFieldData(listItemVM: AddonListItemVM) -> "SupportsLessThan":
			return self._getAddonAttrText(listItemVM, self._sortByModelFieldName)

		def _containsTerm(detailsVM: AddonListItemVM, term: str) -> bool:
			term = term.casefold()
			model = detailsVM.model
			return (
				term in model.displayName.casefold()
				or term in model.description.casefold()
				or term in model.publisher.casefold()
			)

		filtered = (
			vm for vm in self._addons.values()
			if self._filterString is None or _containsTerm(vm, self._filterString)
		)
		filteredSorted = list([
			vm.Id for vm in sorted(filtered, key=_getSortFieldData)
		])
		return filteredSorted

	def _tryPersistSelection(
			self,
			newOrder: List[str],
	) -> Optional[str]:
		"""Get the ID of the selection in new order, _addonsFilteredOrdered should not have changed yet.
		"""
		selectedIndex = self.getSelectedIndex()
		selectedId = self.selectedAddonId
		if selectedId in newOrder:
			# nothing else to do, selection doesn't have to change.
			log.debug(f"Selected Id in new order {selectedId}")
			return selectedId
		elif not newOrder:
			log.debug(f"No entries in new order")
			# no entries after filter, select None
			return None
		elif selectedIndex is not None:
			# select the addon at the closest index
			oldMaxIndex: int = len(self._addonsFilteredOrdered) - 1
			oldIndexNorm: float = selectedIndex / oldMaxIndex  # min-max scaling / normalization
			newMaxIndex: int = len(newOrder) - 1
			approxNewIndex = int(oldIndexNorm * newMaxIndex)
			newSelectedIndex = max(0, min(approxNewIndex, newMaxIndex))
			log.debug(
				f"Approximate from position "
				f"oldSelectedIndex: {selectedIndex}, "
				f"oldMaxIndex: {oldMaxIndex}, "
				f"newSelectedIndex: {newSelectedIndex}, "
				f"newMaxIndex: {newMaxIndex}"
			)
			return newOrder[newSelectedIndex]
		elif self.lastSelectedAddonId in newOrder:
			log.debug(f"lastSelected in new order: {self.lastSelectedAddonId}")
			return self.lastSelectedAddonId
		elif newOrder:
			# if there is any addon select it.
			return newOrder[0]
		else:
			log.debug(f"No selection")
			# no selection.
			return None

	def _updateAddonListing(self):
		newOrder = self._getFilteredSortedIds()
		self.selectedAddonId = self._tryPersistSelection(newOrder)
		if self.selectedAddonId:
			self.lastSelectedAddonId = self.selectedAddonId
		self._addonsFilteredOrdered = newOrder

	def applyFilter(self, filterText: str) -> None:
		oldOrder = self._addonsFilteredOrdered
		if not filterText:
			filterText = None
		self._filterString = filterText
		self._updateAddonListing()
		if oldOrder != self._addonsFilteredOrdered:
			# ensure calling on the main thread.
			core.callLater(delay=0, callable=self.updated.notify)


class AddonActionVM:
	""" Actions/behaviour that can be embedded within other views/viewModels that can apply to a single
	L{AddonListItemVM}.
	Use the L{AddonActionVM.updated} extensionPoint.Action to be notified about changes.
	E.G.:
	- Updates within the AddonListItemVM (perhaps changing the action validity)
	- Entirely changing the AddonListItemVM action will be applied to, the validity can be checked for the new
	item.
	"""
	def __init__(
			self,
			displayName: str,
			actionHandler: typing.Callable[[AddonListItemVM, ], None],
			validCheck: typing.Callable[[AddonListItemVM, ], bool],
			listItemVM: Optional[AddonListItemVM],
	):
		"""
		@param displayName: Translated string, to be displayed to the user. Should describe the action / behaviour.
		@param actionHandler: Call when the action is triggered.
		@param validCheck: Is the action valid for the current listItemVM
		@param listItemVM: The listItemVM this action will be applied to. L{updated} notifies of modification.
		"""
		self.displayName: str = displayName
		self.actionHandler: typing.Callable[[AddonListItemVM, ], None] = actionHandler
		self._validCheck: typing.Callable[[AddonListItemVM, ], bool] = validCheck
		self._listItemVM: Optional[AddonListItemVM] = listItemVM
		if listItemVM:
			listItemVM.updated.register(self._listItemChanged)
		self.updated = extensionPoints.Action()
		"""Notify of changes to the action"""

	def _listItemChanged(self, addonListItemVM: AddonListItemVM):
		"""Something inside the AddonListItemVM has changed"""
		assert self._listItemVM == addonListItemVM
		self._notify()

	def _notify(self):
		# ensure calling on the main thread.
		core.callLater(delay=0, callable=self.updated.notify, addonActionVM=self)

	@property
	def isValid(self) -> bool:
		return self._validCheck(self._listItemVM)

	@property
	def listItemVM(self) -> AddonListItemVM:
		return self._listItemVM

	@listItemVM.setter
	def listItemVM(self, listItemVM):
		if self._listItemVM == listItemVM:
			return
		if self._listItemVM:
			self._listItemVM.updated.unregister(self._listItemChanged)
		if listItemVM:
			listItemVM.updated.register(self._listItemChanged)
		self._listItemVM = listItemVM
		self._notify()


class AddonStoreVM:
	def __init__(self, dataManager: DataManager):
		self._dataManager: DataManager = dataManager
		self.hasError = extensionPoints.Action()
		self._addons: List[AddonDetailsModel] = []
		self.listVM: AddonListVM = AddonListVM(
			addons=[
				AddonListItemVM(model=a, status=AvailableAddonStatus.AVAILABLE)
				for a in self._addons
			]
		)
		self.detailsVM: AddonDetailsVM = AddonDetailsVM(
			listItem=self.listVM.getSelection()
		)
		self.actionVMList = self._makeActionsList()
		self.listVM.selectionChanged.register(self._onSelectedItemChanged)

	def _onSelectedItemChanged(self):
		selectedVM = self.listVM.getSelection()
		log.debug(f"Setting selection: {selectedVM}")
		self.detailsVM.listItem = selectedVM
		for action in self.actionVMList:
			action.listItemVM = selectedVM

	def isInstallActionValid(self, listItemVM: Optional[AddonListItemVM]) -> bool:
		return (
			listItemVM is not None
			and listItemVM.status == AvailableAddonStatus.AVAILABLE
		)

	def _makeActionsList(self):
		selectedListItem: Optional[AddonListItemVM] = self.listVM.getSelection()
		return [
			AddonActionVM(
				# Translators: Label for a button that installs the selected addon
				displayName=_("Install"),
				actionHandler=self.installAddon,
				validCheck=self.isInstallActionValid,
				listItemVM=selectedListItem
			),
		]

	def _installAddonInBG(self, listItemVM: AddonListItemVM):
		try:
			for status in self._doInstallAddon(listItemVM):
				log.debug(f"{listItemVM.Id} status: {status}")
				listItemVM.status = status
		except TranslatedError as e:
			log.debugWarning(f"Error during installation of {listItemVM.Id}", exc_info=True)
			# ensure calling on the main thread.
			core.callLater(delay=0, callable=self.hasError.notify, error=e)

	def installAddon(self, listItemVM: AddonListItemVM):
		threading.Thread(
			target=self._installAddonInBG,
			name="install addon",
			args=[listItemVM],
		).start()

	def _doInstallAddon(self, addon: AddonListItemVM) -> typing.Generator[AvailableAddonStatus, None, None]:
		yield AvailableAddonStatus.DOWNLOADING
		fileDownloaded = self._dataManager.downloadAddonFile(addon.model)
		if fileDownloaded is None:
			yield AvailableAddonStatus.DOWNLOAD_FAILED
			raise TranslatedError(
				displayMessage=pgettext(
					"addonStore",
					"Unable to download addon."
				)
			)
		yield AvailableAddonStatus.INSTALLING
		try:
			installAddon(fileDownloaded)
		except TranslatedError as e:
			yield AvailableAddonStatus.INSTALL_FAILED
			raise e
		yield AvailableAddonStatus.INSTALLED

	def refresh(self):
		threading.Thread(target=self._getAddonsInBG, name="getAddonData").start()

	def _getAddonsInBG(self):
		log.debug("getting addons in the background")
		addons = self._dataManager.getLatestAvailableAddons()
		log.debug("completed getting addons in the background")
		if self._addons == addons:  # no change
			log.debug("no change in addons")
			return
		self._addons = addons
		self.listVM.resetListItems([
			AddonListItemVM(
				model=addon,
				status=AvailableAddonStatus.AVAILABLE
			) for addon in addons
		])
		self.detailsVM.listItem = self.listVM.getSelection()
		log.debug("completed refresh")


class TranslatedError(Exception):
	def __init__(self, displayMessage: str):
		"""
		@param displayMessage: A translated message, to be display to the user.
		"""
		self.displayMessage = displayMessage


def getAddonBundleToInstallIfValid(addonPath: str) -> addonHandler.AddonBundle:
	"""
	@param addonPath: path to the 'nvda-addon' file.
	@return: the addonBundle, if valid
	@raise TranslatedError if the addon bundle is invalid / incompatible.
	"""
	try:
		bundle = addonHandler.AddonBundle(addonPath)
	except addonHandler.AddonError:
		log.error("Error opening addon bundle from %s" % addonPath, exc_info=True)
		raise TranslatedError(
			# Translators: The message displayed when an error occurs when opening an add-on package for adding.
			displayMessage=pgettext(
				"addonStore",
				"Failed to open add-on package file at %s - missing file or invalid file format"
			) % addonPath
		)

	if (
		not addonVersionCheck.hasAddonGotRequiredSupport(bundle)
		or not addonVersionCheck.isAddonTested(bundle)
	):
		# This should not happen, only compatible add-ons are intended to be presented in the add-on store.
		raise TranslatedError(
			# Translators: The message displayed when an add-on is not supported by this version of NVDA.
			displayMessage=pgettext(
				"addonStore",
				"Add-on not supported %s"
			) % addonPath
		)
	return bundle


def getPreviouslyInstalledAddonById(addonId: str) -> Optional[addonHandler.Addon]:
	for addon in addonHandler.getAvailableAddons():
		if (
			not addon.isPendingRemove
			# name is the primary identifier within add-on manifests.
			and addonId.lower() == addon.manifest['name'].lower()
		):
			return addon
	return None


def installAddon(addonPath: str) -> None:
	""" Installs the addon at path.
	Any error messages / warnings are presented to the user via a GUI message box.
	If attempting to install an addon that is pending removal, it will no longer be pending removal.
	@note See also L{gui.addonGui.installAddon}
	@raise TranslatedError on failure
	"""
	bundle = getAddonBundleToInstallIfValid(addonPath)
	prevAddon = getPreviouslyInstalledAddonById(addonId=bundle.name)

	try:
		if prevAddon:
			prevAddon.requestRemove()
		addonHandler.installAddonBundle(bundle)
	except addonHandler.AddonError:  # Handle other exceptions as they are known
		log.error("Error installing addon bundle from %s" % addonPath, exc_info=True)
		raise TranslatedError(
			# Translators: The message displayed when an error occurs when installing an add-on package.
			displayMessage=pgettext(
				"addonStore",
				"Failed to install add-on from %s"
			) % addonPath
		)
