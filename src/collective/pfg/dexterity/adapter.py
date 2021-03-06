# -*- coding: utf-8 -*-
"""Dexterity content creation adapter for PloneFormGen
"""
from AccessControl import ClassSecurityInfo
from AccessControl.interfaces import IOwned
from AccessControl.SecurityManagement import getSecurityManager
from AccessControl.SecurityManagement import newSecurityManager
from AccessControl.SecurityManagement import setSecurityManager
from Acquisition import aq_parent
from collective.pfg.dexterity.config import PROJECTNAME
from collective.pfg.dexterity.interfaces import IDexterityContentAdapter
from plone.dexterity.interfaces import IDexterityFTI
from plone.dexterity.utils import addContentToContainer
from plone.dexterity.utils import createContent
from plone.dexterity.utils import getAdditionalSchemata
from plone.memoize import ram
from Products.Archetypes import atapi
from Products.Archetypes.Widget import SelectionWidget
from Products.ATContentTypes.content.schemata import finalizeATCTSchema
from Products.CMFCore.permissions import ModifyPortalContent
from Products.CMFCore.utils import getToolByName
from Products.DataGridField.DataGridField import DataGridField
from Products.DataGridField.DataGridWidget import DataGridWidget
from Products.DataGridField.SelectColumn import SelectColumn
from Products.PloneFormGen.config import FORM_ERROR_MARKER
from Products.PloneFormGen.content.actionAdapter import FormActionAdapter
from Products.PloneFormGen.content.actionAdapter import FormAdapterSchema
from Products.PloneFormGen.interfaces import IPloneFormGenActionAdapter
from Products.PloneFormGen.interfaces import IPloneFormGenField
from time import time
from z3c.form.interfaces import IDataConverter
from z3c.form.interfaces import IDataManager
from z3c.form.interfaces import IFieldWidget
from z3c.form.interfaces import IFormLayer
from ZODB.POSException import ConflictError
from zope.annotation.interfaces import IAnnotations
from zope.component import getMultiAdapter
from zope.component import getUtility
from zope.globalrequest import getRequest
from zope.i18nmessageid import MessageFactory as ZopeMessageFactory
from zope.i18nmessageid import Message
from zope.interface import alsoProvides
from zope.interface import implementer
from zope.proxy import ProxyBase
from zope.schema import Choice
from zope.schema import Date
from zope.schema import Datetime
from zope.schema import List
from zope.schema import Set
from zope.schema import TextLine
from zope.schema.interfaces import IVocabularyFactory

import logging
import re


try:
    from Products.Archetypes.Widget import RelatedItemsWidget
    HAS_RELATED_ITEMS_WIDGET = True
except ImportError:
    from archetypes.referencebrowserwidget.widget import ReferenceBrowserWidget
    HAS_RELATED_ITEMS_WIDGET = False

try:
    from collective.z3cform.datetimewidget import DateWidget
    from collective.z3cform.datetimewidget import DatetimeWidget
except ImportError:
    class DateWidget(object):
        pass

    class DatetimeWidget(object):
        pass


_ = ZopeMessageFactory('collective.pfg.dexterity')


class LinkReprProxy(ProxyBase):

    def __unicode__(self):
        title = self.Title()
        if isinstance(title, bytes):
            return u'<a href="{0:s}">{1:s}</a>'.format(
                self.absolute_url(), title.decode('utf-8', 'ignore'))
        else:
            return u'<a href="{0:s}">{1:s}</a>'.format(
                self.absolute_url(), title)

LOG = logging.getLogger('collective.pfg.dexterity')

TARGET_INTERFACES = (
    'Products.ATContentTypes.interfaces.folder.IATFolder',
    'collective.pfg.dexterity.interfaces.IDexterityContentAdapter',
    'plone.dexterity.interfaces.IDexterityContainer'
)


DexterityContentAdapterSchema = FormAdapterSchema.copy() + atapi.Schema((
    atapi.StringField(
        'createdType',
        required=True,
        write_permission=ModifyPortalContent,
        read_permission=ModifyPortalContent,
        storage=atapi.AnnotationStorage(),
        searchable=False,
        vocabulary='listTypes',
        widget=SelectionWidget(
            label=_('created_type_label',
                    default=u'Content type'),
            description=_('created_type_help',
                          default=(u'Select the type of new content '
                                   u'to be created.'))
        )
    ),
    (HAS_RELATED_ITEMS_WIDGET and atapi.StringField(
        'targetFolder',
        required=True,
        write_permission=ModifyPortalContent,
        read_permission=ModifyPortalContent,
        storage=atapi.AnnotationStorage(),
        searchable=False,
        widget=RelatedItemsWidget(
            label=_('target_folder_label',
                    default=u'Target folder'),
            description=_('target_folder_help',
                          default=(u'Select the target folder, where '
                                   u'created new content should be '
                                   u'placed. Please, make sure that the '
                                   u'folder allows adding '
                                   u'content of the selected type.'))
        ),
        relationship='targetFolder',
        multiValued=False
    ) or atapi.ReferenceField(
        'targetFolder',
        required=True,
        write_permission=ModifyPortalContent,
        read_permission=ModifyPortalContent,
        storage=atapi.AnnotationStorage(),
        searchable=False,
        widget=ReferenceBrowserWidget(
            label=_('target_folder_label',
                    default=u'Target folder'),
            description=_('target_folder_help',
                          default=(u'Select the target folder, where created '
                                   u'new content should be placed. Please, '
                                   u'make sure that the folder allows adding '
                                   u'content of the selected type.')),
            base_query={'object_provides': TARGET_INTERFACES}
        ),
        relationship='targetFolder',
        multiValued=False
    )),
    atapi.BooleanField(
        'giveOwnership',
        required=False,
        write_permission=ModifyPortalContent,
        read_permission=ModifyPortalContent,
        storage=atapi.AnnotationStorage(),
        searchable=False,
        widget=atapi.BooleanWidget(
            label=_('give_ownership_label',
                    default=u'Give ownership'),
            description=_('give_ownership_help',
                          default=(u'Select this to transfer the ownership of '
                                   u'created content for the logged-in user. '
                                   u'This has no effect for anonymous users.'))
        ),
        default=False
    ),
    DataGridField(
        'fieldMapping',
        required=False,
        write_permission=ModifyPortalContent,
        read_permission=ModifyPortalContent,
        storage=atapi.AnnotationStorage(),
        searchable=False,
        allow_delete=True,
        allow_insert=True,
        allow_reorder=True,
        columns=('form', 'content'),
        widget=DataGridWidget(
            label=_('field_mapping_label',
                    default=u'Field mapping'),
            description=_('field_mapping_help',
                          default=u'Map form fields to field of the '
                                  u'selected content type. Please note, '
                                  u'that you must first select the '
                                  u'content type, then save this adapter, '
                                  u"and only then you'll be able to see the "
                                  u'fields of the selected content type.'),
            columns={
                'form': SelectColumn(
                    _('field_mapping_form_label',
                      default=u'Select a form field'),
                    vocabulary='listFormFields'),
                'content': SelectColumn(
                    _('field_mapping_content_label',
                      default=u'to be mapped to a content field.'),
                    vocabulary='listContentFields')
            },
        )
    ),
    atapi.StringField(
        'workflowTransition',
        required=False,
        write_permission=ModifyPortalContent,
        read_permission=ModifyPortalContent,
        storage=atapi.AnnotationStorage(),
        searchable=False,
        vocabulary='listTransitions',
        widget=SelectionWidget(
            label=_('workflow_transition_label',
                    default=u'Trigger workflow transition'),
            description=_('workflow_transition_help',
                          default=(u'You may select a workflow transition '
                                   u'to be triggered after new content is '
                                   u'created.'))
        ),
    ),
    atapi.StringField(
        'createdURL',
        required=False,
        write_permission=ModifyPortalContent,
        read_permission=ModifyPortalContent,
        storage=atapi.AnnotationStorage(),
        searchable=False,
        vocabulary='listOptionalFormFields',
        widget=SelectionWidget(
            label=_('create_url_label',
                    default=u'Save URL'),
            description=_('created_url_help',
                          default=(u'You may select a form field to be '
                                   u'filled with the URL of the created '
                                   u'content. The field may be hidden on '
                                   u'the original form.'))
        )
    )
))
finalizeATCTSchema(DexterityContentAdapterSchema)

DexterityContentAdapterSchema['title'].storage =\
    atapi.AnnotationStorage()
DexterityContentAdapterSchema['description'].storage =\
    atapi.AnnotationStorage()


def as_owner(func):
    """Decorator for executing actions as the context owner
    """

    @ram.cache(lambda method, context, owner: (owner.getId(), time() // 60))
    def wrapped(context, owner):
        users = context.getPhysicalRoot().restrictedTraverse(
            getToolByName(context, 'acl_users').getPhysicalPath())
        return owner.__of__(users)

    def wrapper(context, *args, **kwargs):
        owner = IOwned(context).getOwner()  # get the owner
        old_security_manager = getSecurityManager()
        newSecurityManager(getRequest(), wrapped(context, owner))
        try:
            return func(context, *args, **kwargs)
        except ConflictError:
            raise
        finally:
            # Note that finally is also called before return
            setSecurityManager(old_security_manager)
    return wrapper


@implementer(IPloneFormGenActionAdapter, IDexterityContentAdapter)
class DexterityContentAdapter(FormActionAdapter):
    """Dexterity content creation adapter for PloneFormGen
    """

    security = ClassSecurityInfo()

    portal_type = 'Dexterity Content Adapter'
    schema = DexterityContentAdapterSchema

    _at_rename_after_creation = True

    title = atapi.ATFieldProperty('title')
    description = atapi.ATFieldProperty('description')

    createdType = atapi.ATFieldProperty('createdType')
    targetFolder = atapi.ATFieldProperty('targetFolder')
    fieldMapping = atapi.ATFieldProperty('fieldMapping')
    workflowTransition = atapi.ATFieldProperty('workflowTransition')

    @as_owner
    def _createAsOwner(self, createdType, **kw):
        return createContent(createdType, **kw)

    @as_owner
    def _addContentToContainerAsOwner(self, targetFolder, obj):
        return addContentToContainer(targetFolder, obj, checkConstraints=True)

    @as_owner
    def _deleteAsOwner(self, container, obj):
        container.manage_delObjects([obj.getId()])

    @as_owner  # noqa
    def _setAsOwner(self, context, field, value):
        # Do some trivial transforms
        def transform(value, field):
            if isinstance(field, Set) and isinstance(value, unicode):
                value = set((value,))
            elif isinstance(field, Set) and isinstance(value, tuple):
                value = set(value)
            elif isinstance(field, Set) and isinstance(value, list):
                value = set(value)
            elif isinstance(field, List) and isinstance(value, unicode):
                value = list((value,))
            elif (isinstance(field, Choice) and
                  isinstance(value, list) and len(value) == 1):
                value = value[0]
            return value

        # Try to set the value on created object
        value = transform(value, field)
        try:
            # 2) Try your luck with z3c.form adapters
            widget = getMultiAdapter((field, getRequest()), IFieldWidget)
            converter = IDataConverter(widget)
            dm = getMultiAdapter((context, field), IDataManager)

            # Convert datetimes to collective.z3cform.datetimewidget-compatible
            if isinstance(field, Datetime) and isinstance(widget, DatetimeWidget):  # noqa
                value = re.compile('\d+').findall(value)

            # Convert dates to collective.z3cform.datetimewidget-compatible
            if isinstance(field, Date) and isinstance(widget, DateWidget):
                value = re.compile('\d+').findall(value[:10])  # YYYY-MM-DD
            # Convert dates to plone.app.z3cform.widgets.datewidget-compatible
            elif isinstance(field, Date):
                value = value.split()[0]

            dm.set(converter.toFieldValue(value))
        except ConflictError:
            raise
        except Exception, e:
            try:
                # 1) Try to set it directly
                bound_field = field.bind(context)
                bound_field.validate(value)
                bound_field.set(context, value)
            except ConflictError:
                raise
            except Exception:
                LOG.error(e)
                return u'An unexpected error: {0:s} {1:s}'.format(
                    e.__class__, e)

    @as_owner
    def _doActionAsOwner(self, wftool, context, transition):
        try:
            wftool.doActionFor(context, transition)
        except ConflictError:
            raise
        except Exception, e:
            LOG.error(e)
            return u'An unexpected error: {0:s} {1:s}'.format(e.__class__, e)

    @as_owner
    def _reindexAsOwner(self, context):
        context.reindexObject()

    if HAS_RELATED_ITEMS_WIDGET:

        @security.private
        def getTargetFolder(self):
            value = getattr(self.aq_base, 'targetFolder', '')
            if value:
                for brain in self.portal_catalog.unrestrictedSearchResults(
                        UID=value):
                    return LinkReprProxy(brain._unrestrictedGetObject())
            return None

        @security.private
        def setTargetFolder(self, value):
            setattr(self.aq_base, 'targetFolder', value)

    @security.public  # noqa
    def onSuccess(self, fields, REQUEST=None):
        createdType = self.getCreatedType()
        targetFolder = self.getTargetFolder()
        fieldMapping = self.getFieldMapping()
        giveOwnership = self.getGiveOwnership()
        workflowTransition = self.getWorkflowTransition()
        urlField = self.getCreatedURL()

        # Unwrap getTargetFolder proxy on Plone 5
        targetFolder = aq_parent(targetFolder)[targetFolder.getId()]

        # Support for content adapter chaining
        annotations = IAnnotations(REQUEST)
        if targetFolder.portal_type == 'Dexterity Content Adapter':
            targetFolder =\
                annotations['collective.pfg.dexterity'][targetFolder.getId()]
            # TODO: ^ We should fail more gracefully when the annotation
            # doesn't exist, but now we just let the transaction fail
            # and 500 Internal Error to be returned. (That's because a
            # previous adapter may have created content and we don't want
            # it to be persisted.)

        values = {}

        plone_utils = getToolByName(self, 'plone_utils')
        site_encoding = plone_utils.getSiteEncoding()

        # Parse values from the submission
        alsoProvides(REQUEST, IFormLayer)  # let us to find z3c.form adapters
        for mapping in fieldMapping:
            field = self._getDexterityField(createdType, mapping['content'])

            if '{0:s}_file'.format(mapping['form']) in REQUEST:
                value = REQUEST.get('{0:s}_file'.format(mapping['form']))
            else:
                value = REQUEST.get(mapping['form'], None)
                # Convert strings to unicode
                if isinstance(value, str):
                    value = unicode(value, site_encoding, 'replace')
                if (isinstance(value, list) and
                        all([isinstance(v, str) for v in value])):
                    value = [unicode(v, site_encoding, 'replace')
                             for v in value]

            # Apply a few controversial convenience heuristics
            if isinstance(field, TextLine) and isinstance(value, unicode):
                # 1) Multiple text lines into the same field
                old_value = values.get(mapping['content'])
                if old_value and value:
                    value = u' '.join((old_value[1], value))
            elif isinstance(field, List) and isinstance(value, unicode):
                # 2) Split keyword (just a guess) string into list
                value = value.replace(u',', u'\n')
                value = [s.strip() for s in value.split(u'\n') if s]

            values[mapping['content']] = (field, value)

        # Create content with parsed title (or without it)
        try:
            # README: id for new content will be choosed by
            # INameChooser(container).chooseName(None, object),
            # so you should provide e.g. INameFromTitle adapter
            # to generate a custom id
            if 'title' in values:
                context = self._createAsOwner(createdType,
                                              title=values.pop('title')[1])
            else:
                context = self._createAsOwner(createdType)
        except ConflictError:
            raise
        except Exception, e:
            LOG.error(e)
            return {
                FORM_ERROR_MARKER: u'An unexpected error: {0:s} {1:s}'.format(
                    e.__class__, e)
            }

        # Set all parsed values for the created content
        for field, value in values.values():
            error_msg = self._setAsOwner(context, field, value)
            if error_msg:
                return {FORM_ERROR_MARKER: error_msg}

        # Add into container
        context = self._addContentToContainerAsOwner(targetFolder, context)

        # Give ownership for the logged-in submitter, when that's enabled
        if giveOwnership:
            mtool = getToolByName(self, 'portal_membership')
            if not mtool.isAnonymousUser():
                member = mtool.getAuthenticatedMember()
                if 'creators' in context.__dict__:
                    context.creators = (member.getId(),)
                IOwned(context).changeOwnership(member.getUser(), recursive=0)
                context.manage_setLocalRoles(member.getId(), ['Owner', ])

        # Trigger a worklfow transition when set
        if workflowTransition:
            wftool = getToolByName(self, 'portal_workflow')
            error_msg = self._doActionAsOwner(wftool, context,
                                              workflowTransition)
            if error_msg:
                self._deleteAsOwner(targetFolder, context)
                return {FORM_ERROR_MARKER: error_msg}

        # Reindex at the end
        self._reindexAsOwner(context)

        # Set URL to the created content
        if urlField:
            REQUEST.form[urlField] = context.absolute_url()

        # Store created content also as an annotation
        if 'collective.pfg.dexterity' not in annotations:
            annotations['collective.pfg.dexterity'] = {}
        annotations['collective.pfg.dexterity'][self.getId()] = context

    @security.private
    def listTypes(self):
        types = getToolByName(self, 'portal_types')
        dexterity = [(fti.id, fti.title) for fti in types.values()
                     if IDexterityFTI.providedBy(fti)]
        return atapi.DisplayList(dexterity)

    @security.private
    def listFormFields(self):
        fields = [(obj.getId(), obj.title_or_id())
                  for obj in self.aq_parent.objectValues()
                  if IPloneFormGenField.providedBy(obj)]
        return atapi.DisplayList(fields)

    @security.private
    def listOptionalFormFields(self):
        fields = [(obj.getId(), obj.title_or_id())
                  for obj in self.aq_parent.objectValues()
                  if IPloneFormGenField.providedBy(obj)]
        return atapi.DisplayList([(u'', _(u"Don't save"))] + fields)

    def _getDexterityFields(self, portal_type):
        fti = getUtility(IDexterityFTI, name=portal_type)
        schema = fti.lookupSchema()
        fields = {}
        for name in schema:
            fields[name] = schema[name]
        for schema in getAdditionalSchemata(portal_type=portal_type):
            for name in schema:
                fields[name] = schema[name]
        return fields

    def _getDexterityField(self, portal_type, name):
        return self._getDexterityFields(portal_type).get(name, None)

    @security.private
    def listContentFields(self):
        types = getToolByName(self, 'portal_types')
        createdType = self.getCreatedType()

        def smart_title(title, key):
            if not isinstance(title, Message):
                return u'{0:s} ({1:s})'.format(title, key)
            else:
                # Don't brake i18n messages
                return title

        if createdType in types.keys():
            mapping = self._getDexterityFields(createdType)
            fields = [(key, smart_title(mapping[key].title, key))
                      for key in mapping]
        else:
            fields = []
        return atapi.DisplayList(fields)

    @security.private
    def listTransitions(self):
        types = getToolByName(self, 'portal_types')
        createdType = self.getCreatedType()
        if createdType in types.keys():
            workflows = getToolByName(self, 'portal_workflow')
            candidates = []
            transitions = []
            for workflow in [workflows.get(key) for key in
                             workflows.getChainForPortalType(createdType)
                             if key in workflows.keys()]:
                candidates.extend(
                    workflow.states.get(workflow.initial_state).transitions)
            for transition in set(candidates):
                transitions.append((transition,
                                    workflows.getTitleForTransitionOnType(
                                        transition, createdType)))
        else:
            vocabulary = getUtility(
                IVocabularyFactory,
                name=u'plone.app.vocabularies.WorkflowTransitions'
            )(self)
            transitions = [(term.value, term.title) for term in vocabulary]
        return atapi.DisplayList(
            [(u'', _(u'No transition'))] +
            sorted(transitions, lambda x, y: cmp(x[1].lower(),
                                                 y[1].lower()))
        )

atapi.registerType(DexterityContentAdapter, PROJECTNAME)
