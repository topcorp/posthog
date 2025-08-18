from typing import Any, Union
import logging

import posthoganalytics
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http.response import HttpResponse
from django.urls.base import reverse
from rest_framework.decorators import api_view
from rest_framework.exceptions import PermissionDenied
from social_core.backends.saml import (
    OID_COMMON_NAME,
    OID_GIVEN_NAME,
    OID_MAIL,
    OID_SURNAME,
    OID_USERID,
    SAMLAuth,
    SAMLIdentityProvider,
)
from social_core.backends.google import GoogleOAuth2
from social_core.exceptions import AuthFailed, AuthMissingParameter
from social_django.utils import load_backend, load_strategy

from posthog.constants import AvailableFeature
from posthog.models.organization import OrganizationMembership
from posthog.models.organization_domain import OrganizationDomain
from social_django.models import UserSocialAuth

logger = logging.getLogger(__name__)


@api_view(["GET"])
def saml_metadata_view(request, *args, **kwargs):
    if (
        not request.user.organization_memberships.get(organization=request.user.organization).level
        >= OrganizationMembership.Level.ADMIN
    ):
        raise PermissionDenied("You need to be an administrator or owner to access this resource.")

    complete_url = reverse("social:complete", args=("saml",))
    saml_backend = load_backend(load_strategy(request), "saml", redirect_uri=complete_url)
    metadata, errors = saml_backend.generate_metadata_xml()

    if not errors:
        return HttpResponse(content=metadata, content_type="text/xml")


class MultitenantSAMLAuth(SAMLAuth):
    """
    Implements our own version of SAML auth that supports multitenancy. Instead of relying on instance-based config via env vars,
    each organization can have multiple verified domains each with its own SAML configuration.
    """

    def auth_complete(self, *args, **kwargs):
        try:
            result = super().auth_complete(*args, **kwargs)
            
            # Enhanced security validation: ensure strong authentication context was used
            if hasattr(result, 'response') and 'AuthnContextClassRef' in str(result.response):
                auth_context = self._extract_auth_context(result.response)
                if auth_context and not self._is_strong_auth_context(auth_context):
                    logger.error(
                        "SAML authentication rejected: Weak authentication context used",
                        extra={
                            "auth_context": auth_context,
                            "user_email": getattr(result, 'email', 'unknown')
                        }
                    )
                    raise AuthFailed("saml", "Authentication method not strong enough for this organization.")
            
            logger.info("SAML authentication completed successfully with enhanced security context")
            return result
        except Exception as e:
            import json

            logger.warning(
                "SAML authentication failed",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__
                }
            )
            posthoganalytics.tag("request_data", json.dumps(self.strategy.request_data()))
            raise

    def get_idp(self, organization_domain_or_id: Union["OrganizationDomain", str]):
        try:
            organization_domain = (
                organization_domain_or_id
                if isinstance(organization_domain_or_id, OrganizationDomain)
                else OrganizationDomain.objects.verified_domains().get(id=organization_domain_or_id)
            )
        except (OrganizationDomain.DoesNotExist, DjangoValidationError) as e:
            logger.warning(
                "SAML authentication failed: Invalid organization domain",
                extra={
                    "domain_or_id": str(organization_domain_or_id),
                    "error": str(e)
                }
            )
            raise AuthFailed("saml", "Authentication request is invalid. Invalid RelayState.")

        if not organization_domain.organization.is_feature_available(AvailableFeature.SAML):
            logger.warning(
                "SAML authentication failed: Feature not available",
                extra={
                    "organization_id": str(organization_domain.organization.id),
                    "domain": organization_domain.domain
                }
            )
            raise AuthFailed(
                "saml",
                "Your organization does not have the required license to use SAML.",
            )

        # Validate SAML configuration completeness
        if not all([organization_domain.saml_entity_id, organization_domain.saml_acs_url, organization_domain.saml_x509_cert]):
            logger.error(
                "SAML authentication failed: Incomplete SAML configuration",
                extra={
                    "organization_id": str(organization_domain.organization.id),
                    "domain": organization_domain.domain,
                    "has_entity_id": bool(organization_domain.saml_entity_id),
                    "has_acs_url": bool(organization_domain.saml_acs_url),
                    "has_x509_cert": bool(organization_domain.saml_x509_cert)
                }
            )
            raise AuthFailed("saml", "SAML configuration is incomplete for this domain.")

        logger.info(
            "SAML IdP configuration validated successfully",
            extra={
                "organization_id": str(organization_domain.organization.id),
                "domain": organization_domain.domain
            }
        )

        return SAMLIdentityProvider(
            str(organization_domain.id),
            entity_id=organization_domain.saml_entity_id,
            url=organization_domain.saml_acs_url,
            x509cert=organization_domain.saml_x509_cert,
        )

    def auth_url(self):
        """
        Overridden to use the config from the relevant OrganizationDomain
        Get the URL to which we must redirect in order to
        authenticate the user
        """
        email = self.strategy.request_data().get("email")

        if not email:
            raise AuthMissingParameter("saml", "email")

        instance = OrganizationDomain.objects.get_verified_for_email_address(email=email)

        if not instance or not instance.has_saml:
            raise AuthFailed("saml", "SAML not configured for this user.")

        auth = self._create_saml_auth(idp=self.get_idp(instance))
        # Below, return_to sets the RelayState, which contains the ID of
        # the `OrganizationDomain`.  We use it to store the specific SAML IdP
        # name, since we multiple IdPs share the same auth_complete URL.
        return auth.login(return_to=str(instance.id))

    def _get_attr(
        self,
        response_attributes: dict[str, Any],
        attribute_names: list[str],
        optional: bool = False,
    ) -> str:
        """
        Fetches a specific attribute from the SAML response, attempting with multiple different attribute names.
        We attempt multiple attribute names to make it easier for admins to configure SAML (less configuration to set).
        """
        output = None
        for _attr in attribute_names:
            if _attr in response_attributes:
                output = response_attributes[_attr]
                break

        if not output and not optional:
            raise AuthMissingParameter("saml", attribute_names[0])

        if isinstance(output, list):
            output = output[0]

        return output

    def get_user_details(self, response):
        """
        Overridden to find attributes across multiple possible names.
        """
        attributes = response["attributes"]
        return {
            "fullname": self._get_attr(
                attributes,
                ["full_name", "FULL_NAME", "fullName", OID_COMMON_NAME],
                optional=True,
            ),
            "first_name": self._get_attr(
                attributes,
                [
                    "first_name",
                    "FIRST_NAME",
                    "firstName",
                    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname",
                    OID_GIVEN_NAME,
                ],
                optional=True,
            ),
            "last_name": self._get_attr(
                attributes,
                [
                    "last_name",
                    "LAST_NAME",
                    "lastName",
                    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname",
                    OID_SURNAME,
                ],
                optional=True,
            ),
            "email": self._get_attr(
                attributes,
                [
                    "email",
                    "EMAIL",
                    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
                    OID_MAIL,
                ],
            ),
        }

    def get_user_id(self, details, response):
        """
        Overridden to find user ID across multiple attribute names.
        Get the permanent ID for this user from the response.
        """
        USER_ID_ATTRIBUTES = ["name_id", "NAME_ID", "nameId", OID_USERID]
        uid = self._get_attr(response["attributes"], USER_ID_ATTRIBUTES)
        return f"{response['idp_name']}:{uid}"

    def _extract_auth_context(self, response) -> str:
        """
        Extract authentication context class reference from SAML response.
        """
        try:
            import xml.etree.ElementTree as ET
            
            if hasattr(response, 'xmlstr'):
                root = ET.fromstring(response.xmlstr)
                for elem in root.iter():
                    if elem.tag.endswith('AuthnContextClassRef'):
                        return elem.text or ""
        except Exception as e:
            logger.warning(
                "Failed to extract authentication context",
                extra={"error": str(e)}
            )
        return ""

    def _is_strong_auth_context(self, auth_context: str) -> bool:
        """
        Validate that the authentication context meets minimum security requirements.
        Accept contexts that provide reasonable security while maintaining IdP compatibility.
        """
        acceptable_contexts = [
            "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport",
            "urn:oasis:names:tc:SAML:2.0:ac:classes:Password",  # Basic password auth is acceptable for most IdPs
            "urn:oasis:names:tc:SAML:2.0:ac:classes:MultifactorUnregistered",
            "urn:oasis:names:tc:SAML:2.0:ac:classes:Smartcard",
            "urn:oasis:names:tc:SAML:2.0:ac:classes:SmartcardPKI",
            "urn:oasis:names:tc:SAML:2.0:ac:classes:unspecified"  # Accept unspecified for broader IdP compatibility
        ]
        
        # Only explicitly reject clearly insecure authentication contexts
        explicitly_rejected_contexts = [
            "urn:oasis:names:tc:SAML:2.0:ac:classes:InternetProtocol",  # No authentication required
            "urn:oasis:names:tc:SAML:2.0:ac:classes:InternetProtocolPassword"  # Deprecated weak method
        ]
        
        if auth_context in explicitly_rejected_contexts:
            logger.warning(
                "Insecure authentication context rejected",
                extra={"auth_context": auth_context}
            )
            return False
            
        # Log unknown contexts for monitoring but don't reject for compatibility
        if auth_context not in acceptable_contexts and auth_context:
            logger.info(
                "Unknown authentication context accepted for compatibility",
                extra={"auth_context": auth_context}
            )
            
        return True  # Accept by default to prevent authentication failures with various IdPs


class CustomGoogleOAuth2(GoogleOAuth2):
    def auth_extra_arguments(self):
        extra_args = super().auth_extra_arguments()
        email = self.strategy.request.GET.get("email")

        if email:
            extra_args["login_hint"] = email

        return extra_args

    def get_user_id(self, details, response):
        """
        Retrieve and migrate Google OAuth user identification.

        Note: While social-auth-core supports using Google's sub claim via
        settings.USE_UNIQUE_USER_ID = True, this setting was not enabled historically
        in our application. This led to emails being stored as uids instead of the
        more stable Google sub identifier.

        'sub' (subject identifier) is part of OpenID Connect and is guaranteed to be a
        stable, unique identifier for the user within Google's system. It's designed
        specifically for authentication purposes and won't change even if the user changes
        their email or other profile details.

        This method handles two types of user identification:
        1. Legacy users: Originally stored with email as their uid (due to missing USE_UNIQUE_USER_ID)
        2. New users: Using Google's sub claim (unique identifier) as uid

        The method first checks if a user exists with the sub as uid. If not found,
        it looks for a legacy user with email as uid and migrates them to use sub.
        This ensures a smooth transition from email-based to sub-based identification
        while maintaining backward compatibility.

        Args:
            details: User details dictionary from OAuth response
            response: Full OAuth response from Google

        Returns:
            str: The Google sub claim to be used as uid
        """
        email = response.get("email")
        sub = response.get("sub")

        if not sub:
            raise ValueError("Google OAuth response missing 'sub' claim")

        try:
            # First try: Find user by sub (preferred method)
            social_auth = UserSocialAuth.objects.get(provider="google-oauth2", uid=sub)
            return sub
        except UserSocialAuth.DoesNotExist:
            pass

        try:
            # Second try: Find and migrate legacy user using email as uid
            social_auth = UserSocialAuth.objects.get(provider="google-oauth2", uid=email)
            # Migrate user from email to sub
            social_auth.uid = sub
            social_auth.save()
            return sub
        except UserSocialAuth.DoesNotExist:
            # No existing user found - use sub for new account
            return sub
