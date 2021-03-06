import logging
import urlparse

import cherrypy
from oic.oauth2 import rndstr
from oic.utils.http_util import SeeOther
from oic.utils.http_util import Response
from saml2.client_base import Base
from saml2.config import ONTS, SPConfig
from saml2.httpbase import ConnectionError, HTTPBase
from saml2.mdstore import MetaDataMDX
from saml2.saml import Issuer, NAMEID_FORMAT_ENTITY, NAMEID_FORMAT_PERSISTENT, NAMEID_FORMAT_TRANSIENT
from saml2.samlp import AuthnRequest
from saml2.samlp import NameIDPolicy
from saml2.time_util import instant
from saml2 import BINDING_HTTP_REDIRECT, BINDING_HTTP_POST, sigver
from saml2.s_utils import sid
from saml2 import md
from saml2.attribute_converter import ac_factory

from log_utils import log_internal
from svs.cherrypy_util import response_to_cherrypy
from svs.message_utils import abort_with_client_error, negative_transaction_response
from svs.filter import get_affiliation_function, PERSISTENT_NAMEID, TRANSIENT_NAMEID
from svs.log_utils import log_transaction_idp
from svs.sp_metadata import load_sp_config
from svs.utils import sha1_entity_transform


logger = logging.getLogger(__name__)


class ServiceErrorException(Exception):
    pass


class AuthnFailure(ServiceErrorException):
    pass


class SamlSp(object):
    def __init__(self, conf, disco_srv, force_authn=False,
                 sign_func=None):
        """Constructor for the class.

        :param conf: The SAML SP configuration
        :param disco_srv: The address to the DiscoServer
        :param force_authn: whether to force authentication
        :param sign_func: A function that signs a SAML message
        """
        self.idp_query_param = "IdpQuery"
        self.conf = conf
        self.disco_srv = disco_srv
        self.force_authn = force_authn
        self.sign_func = sign_func

        # returns list of 2-tuples (endpoint, binding)
        acs = self.conf.getattr("endpoints", "sp")["assertion_consumer_service"]
        self.response_binding = acs[0][1]
        self.response_url = acs[0][0]

        # Can be regarded as static, will seldom if ever change
        _cargs = {
            "format": self.conf.getattr("name_id_format", "sp")[0],
            "allow_create": "false"
        }
        self.nameid_policy = NameIDPolicy(**_cargs)

        # This is a simple SP
        self.sp = Base(self.conf)

        # Since I probably didn't send the original request at least I can't
        # count on it.
        self.sp.allow_unsolicited = True
        self.sp.config.entityid = self.conf.entityid
        self.issuer = Issuer(text=self.conf.entityid,
                             format=NAMEID_FORMAT_ENTITY)

    def construct_authn_request(self, idp_entity_id, mds, nameid_policy,
                                assertion_consumer_service_url="",
                                assertion_consumer_service_index=""):
        """Construct the SAML authentication request.

        :param idp_entity_id: Which IdP to send the request to
        :param mds: metadata instance
        :param nameid_policy: A NameIDPolicy instance
        :param assertion_consumer_service_url: Assertion consumer endpoint
        :param assertion_consumer_service_index: Assertion consumer endpoint
        reference
        :return: AuthnRequest instance
        """
        destinations = mds.service(idp_entity_id, "idpsso_descriptor", "single_sign_on_service")

        if destinations is None:
            raise ServiceErrorException("IdP '{}' not known in MDX".format(idp_entity_id))

        # Prioritize HTTP-POST over HTTP-Redirect
        binding = None
        if BINDING_HTTP_POST in destinations:
            binding = BINDING_HTTP_POST
        elif BINDING_HTTP_REDIRECT in destinations:
            binding = BINDING_HTTP_REDIRECT

        if binding is None:
            raise ServiceErrorException("IdP does not support http-post or http-redirect binding")

        location = destinations[binding][0]["location"]
        arg = {"destination": location}

        if assertion_consumer_service_url:
            arg["assertion_consumer_service_url"] = assertion_consumer_service_url
            arg["protocol_binding"] = self.response_binding
        if assertion_consumer_service_index:
            arg["assertion_consumer_service_index"] = assertion_consumer_service_index

        if self.force_authn:
            _force = "true"
        else:
            _force = "false"

        return AuthnRequest(
            id=sid(), version="2.0", issue_instant=instant(),
            issuer=self.issuer, name_id_policy=nameid_policy,
            force_authn=_force, **arg), binding

    def parse_auth_response(self, SAMLResponse, binding):
        """Verifies if the authentication was successful.
        Verifying the RelayState has happened before this method is called.

        :param SAMLResponse: The SAML Response from the IdP
        :return: If the authentication was successful a tuple containing
        the nameID text field and the Identity information. Otherwise an
        AuthnFailure exception.
        """
        if not SAMLResponse:
            log_internal(logger, "Auhtentication response from IdP is missing (maybe authn failure.", None)
            raise AuthnFailure("You are not authorized!")

        _response = self.sp.parse_authn_request_response(SAMLResponse, binding)
        return (_response.assertion.subject.name_id, _response.ava,
                _response.assertion.authn_statement[0].authn_instant, _response.assertion.issuer.text)

    def parse_disco_response(self, query_part):
        """Parse a discovery service response and return only the entity_id of the chosen IdP.

        :param query_part: The query part of the return URL
        :return: The IdP entity ID or "" if none given
        """
        return self.sp.parse_discovery_service_response(query=query_part)

    def disco_query(self, state):
        """Construct the discovery query.

        :return: the URL to redirect the user to the discovery service.
        """
        eid = self.sp.config.entityid
        # returns list of 2-tuples
        dr = self.conf.getattr("endpoints", "sp")["discovery_response"]
        # The first value of the first tuple is the one I want
        ret = dr[0][0]
        ret += "?state={}".format(state)
        # append it to the disco server URL
        loc = self.sp.create_discovery_service_request(
            self.disco_srv, eid, **{"return": ret})
        log_internal(logger, "Discovery service URL: {}".format(self.disco_srv), cherrypy.request,
                     state)
        return loc

    def redirect_to_auth(self, metadata, entity_id, relay_state):
        """Construct the SAML authentication request that will redirect the user to the IdP
        for authentication.

        :param metadata: metadata instance
        :param entity_id: The entity ID of the IdP that should do the
        authentication.
        :param relay_state: A JWE containing state information
        :return: oic.utils.http_util.Response which is either a '303 See Other' in the case of HTTP-Redirect binding
        otherwise a '200 OK' containing with a HTML form as body which is posts the SAML authentication request.
        """
        request, binding = self.construct_authn_request(entity_id, metadata,
                                                        self.nameid_policy,
                                                        self.response_url)
        log_internal(logger, "saml_authn_request {}".format(str(request).replace('\n', '')), None,
                     transaction_id=relay_state)

        if self.sign_func:
            msg_str = self.sign_func(request)
        else:
            msg_str = str(request)

        ht_args = self.sp.apply_binding(binding, msg_str,
                                        request.destination,
                                        relay_state=relay_state)

        if binding == BINDING_HTTP_REDIRECT:
            for param, value in ht_args["headers"]:
                if param == "Location":
                    resp = SeeOther(str(value))
                    break
            else:
                raise ServiceErrorException("Parameter error")
        else:
            resp = Response(''.join(ht_args["data"]), headers=ht_args["headers"])

        return resp


class InAcademiaSAMLBackend(object):
    """The SAML Service Provider (SP) part of InAcademia.

    In reality two different SP's are used to communicate with IdP's with different attribute release policies
    for the SAML metadata NameID attribute.
    """

    def __init__(self, base_url, mdx_url, disco_url):
        """Constructor.

        :param base_url: base url of the entire service
        :param metadata: metadata instance
        :param disco_url: URL to the discovery service
        :return:
        """

        ATTRCONV = ac_factory("")

        http = HTTPBase(verify=False, ca_bundle=None)
        self.metadata = MetaDataMDX(sha1_entity_transform, ONTS.values(), ATTRCONV, mdx_url,
                                    None, None, http, node_name="{}:{}".format(md.EntityDescriptor.c_namespace,
                                                                               md.EntitiesDescriptor.c_tag))

        self.SP = {}
        config = load_sp_config(base_url)
        for sp_key, conf in config.iteritems():
            sp_conf = SPConfig()
            sp_conf.xmlsec_binary = sigver.get_xmlsec_binary()
            sp_conf.metadata = self.metadata
            sp_conf.load(conf)
            self.SP[sp_key] = SamlSp(sp_conf, disco_url, force_authn=True)

    def redirect_to_auth(self, state, scope):
        """Send a redirect to the discovery server.
        """
        sp = self._choose_service_provider(scope)
        location = sp.disco_query(state)
        raise cherrypy.HTTPRedirect(location, 303)

    def _choose_service_provider(self, scope):
        """Choose the correct SP to communicate with the IdP.

        The choice is based on the requested scope from the RP.
        :param SP: dict of SP's with different attribute release policies and persistent or transient name id
        :param scope: requested scope from the RP
        :return: the SP object to use when creating/sending the authentication request to the IdP.
        """

        if PERSISTENT_NAMEID in scope:
            sp_key = PERSISTENT_NAMEID
        else:
            sp_key = TRANSIENT_NAMEID

        return self.SP[sp_key]

    def disco(self, entity_id, transaction_id, transaction_session):
        sp = self._choose_service_provider(transaction_session["scope"])

        idp_entity_id = self._parse_idp_entity_id(entity_id)
        if not self._is_in_edugain(entity_id):
            abort_with_client_error(transaction_id, transaction_session, cherrypy.request, logger,
                                    "Non-edugain IdP '{}' returned from discovery server".format(idp_entity_id))

        log_transaction_idp(logger, cherrypy.request, transaction_id, transaction_session["client_id"], idp_entity_id)

        # Construct the SAML2 AuthenticationRequest and send it
        try:
            return response_to_cherrypy(sp.redirect_to_auth(self.metadata, idp_entity_id, transaction_id))
        except ServiceErrorException as e:
            abort_with_client_error(transaction_id, transaction_session, cherrypy.request, logger,
                                    "Could not create SAML authentication request.",
                                    error_description="Validation could not be completed.", exc_info=True)
        except ConnectionError as e:
            abort_with_client_error(transaction_id, transaction_session, cherrypy.request, logger,
                                    "Could not contact SAML MDQ.",
                                    error_description="Validation could not be completed.", exc_info=True)

    def _is_in_edugain(self, entity_id):
        parsed = urlparse.urlparse(entity_id)
        pqs = urlparse.parse_qs(parsed.query)

        return "true" in pqs.get("inedugain", ["true"])  # TODO re-implement with check at SAMLMetadata

    def _parse_idp_entity_id(self, entity_id):
        parsed = urlparse.urlparse(entity_id)
        # Re-assemble the entity id without query string and fragment identifier
        idp_entity_id = urlparse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))

        return idp_entity_id

    def acs(self, SAMLResponse, binding, transaction_id, transaction_session):
        """
        Handle the SAML Authentication Request (received at the SP's assertion consumer URL).
        :return:
        """
        scope = transaction_session["scope"]
        sp = self._choose_service_provider(scope)
        try:
            name_id, identity, auth_time, idp_entity_id = sp.parse_auth_response(SAMLResponse, binding)
            log_internal(logger, "saml_response name_id={}".format(str(name_id).replace("\n", "")),
                         environ=cherrypy.request, transaction_id=transaction_id,
                         client_id=transaction_session["client_id"])
        except AuthnFailure:
            abort_with_client_error(transaction_id, transaction_session, cherrypy.request, logger,
                                    "User not authenticated at IdP.")
        except Exception as e:
            abort_with_client_error(transaction_id, transaction_session, cherrypy.request, logger,
                                    "Could not parse Authentication Response from IdP.", exc_info=True)

        get_affiliation = get_affiliation_function(scope)
        affiliation = get_affiliation(identity)
        if not affiliation:
            negative_transaction_response(transaction_id, transaction_session,
                                          cherrypy.request, logger, "The user does not have the correct affiliation.",
                                          idp_entity_id)

        if PERSISTENT_NAMEID in scope:
            _user_id = self.get_name_id(name_id, identity)
            if _user_id is None:
                negative_transaction_response(transaction_id, transaction_session, cherrypy.request, logger,
                                              "The users identity could not be provided.",
                                              idp_entity_id)
        else:
            # for transient identifiers, use random string to keep the identifier unique per transaction
            _user_id = rndstr(256)

        return _user_id, affiliation, identity, auth_time, idp_entity_id

    def get_name_id(self, name_id_from_idp, identity):
        """Get the name id.

        If the RP requested a persistent name id, try the following SAML attributes in order:
            1. Persistent name id
            2. eduPersonTargetedId (EPTID)
            3. eduPersonPrincipalName (EPPN)
        :param name_id_from_idp: name id as given by the SAML Auth Response
        :param identity: SAML assertions
        :param scope: requested scope from the RP
        :return: the name id from the IdP or None if an incorrect or no name id at all was returned from the IdP.
        """
        # Use one of NameID (if persistent) or EPTID or EPPN in that order
        if name_id_from_idp.format == NAMEID_FORMAT_PERSISTENT:
            return name_id_from_idp.text
        else:
            for key in ['eduPersonTargetedID', 'eduPersonPrincipalName']:
                if key in identity:
                    return identity[key][0]

        return None
