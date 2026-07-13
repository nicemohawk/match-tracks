"""StoreKit 2 team-features entitlements (V2 §7).

Receipts arrive as StoreKit 2 signed transactions (JWS). Verification is
pluggable via ``app.config['ENTITLEMENT_VERIFIER']`` (tests inject fakes);
the default verifier decodes the JWS payload and, when the ``cryptography``
package is available, verifies the ES256 signature against the x5c leaf
certificate. Production deployments should additionally pin Apple's root
certificates (App Store Server API validation is the gold standard).
"""

import base64
import json
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request

from match_tracks.auth import auth, current_principal, effective_device_id
from match_tracks.memberships import actor_denial
from match_tracks.models import Entitlement

entitlements_blueprint = Blueprint('entitlements', __name__)

TEAM_PRODUCT_FRAGMENT = '.team.'
TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


class VerifierUnavailable(Exception):
    """No way to verify signatures in this deployment."""


class InvalidReceipt(Exception):
    """The JWS is structurally invalid or fails verification."""


def _base64url_decode(segment):
    padding = '=' * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def default_jws_verifier(jws_string):
    """Decode and verify a StoreKit 2 signed transaction.

    Returns ``{'product_id', 'expires_at' (datetime), 'environment'}``.

    Security model: a JWS is only trusted when its x5c certificate chain
    terminates at a PINNED root certificate loaded from the PEM file(s) at
    ``ENTITLEMENT_APPLE_ROOT_CERTS_PATH`` (deployments bundle Apple's root
    CAs there). Verifying against the leaf certificate alone would trust
    whatever certificate the sender minted, so without pinned roots (or the
    ``cryptography`` package) strict mode refuses with VerifierUnavailable
    rather than pretending to verify. ``ENTITLEMENT_ALLOW_UNVERIFIED=True``
    (development only) skips signature checking entirely.
    """
    try:
        header_segment, payload_segment, signature_segment = jws_string.split('.')
        header = json.loads(_base64url_decode(header_segment))
        payload = json.loads(_base64url_decode(payload_segment))
        product_id = payload['productId']
        expires_ms = payload['expiresDate']
        environment = payload.get('environment')
    except (ValueError, KeyError, TypeError) as parse_error:
        raise InvalidReceipt(str(parse_error))

    allow_unverified = current_app.config.get('ENTITLEMENT_ALLOW_UNVERIFIED', False)

    if not allow_unverified:
        try:
            from cryptography import x509  # noqa: F401
        except ImportError:
            raise VerifierUnavailable(
                'cryptography package unavailable and unverified receipts are disabled')

        trusted_roots = _load_pinned_root_certificates()
        if not trusted_roots:
            raise VerifierUnavailable(
                'no pinned Apple root certificates configured '
                '(set ENTITLEMENT_APPLE_ROOT_CERTS_PATH) and unverified receipts '
                'are disabled')

        _verify_jws_signature(header, header_segment, payload_segment,
                              signature_segment, trusted_roots)

    return {
        'product_id': product_id,
        'expires_at': datetime.utcfromtimestamp(expires_ms / 1000.0),
        'environment': environment,
    }


def _load_pinned_root_certificates():
    """Load trusted root certificates from ENTITLEMENT_APPLE_ROOT_CERTS_PATH."""
    import os

    from cryptography import x509

    certs_path = current_app.config.get('ENTITLEMENT_APPLE_ROOT_CERTS_PATH')
    if not certs_path or not os.path.exists(certs_path):
        return []

    pem_paths = []
    if os.path.isdir(certs_path):
        pem_paths = [os.path.join(certs_path, name)
                     for name in sorted(os.listdir(certs_path))
                     if name.endswith(('.pem', '.crt', '.cer'))]
    else:
        pem_paths = [certs_path]

    roots = []
    for pem_path in pem_paths:
        with open(pem_path, 'rb') as pem_file:
            data = pem_file.read()
        try:
            roots.extend(x509.load_pem_x509_certificates(data))
        except ValueError:
            try:
                roots.append(x509.load_der_x509_certificate(data))
            except ValueError:
                continue
    return roots


def _verify_jws_signature(header, header_segment, payload_segment,
                          signature_segment, trusted_roots):
    """Verify the x5c chain to a pinned root, then the ES256 JWS signature."""
    import base64 as base64_module

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        chain = [x509.load_der_x509_certificate(base64_module.b64decode(entry))
                 for entry in header['x5c']]
        if not chain:
            raise InvalidReceipt('empty x5c chain')
    except InvalidReceipt:
        raise
    except Exception as parse_error:
        raise InvalidReceipt(f'malformed x5c chain: {parse_error}')

    try:
        # Each certificate must be signed by the next one up; the last link
        # must be signed by (or be) a pinned root.
        for child, issuer in zip(chain, chain[1:]):
            _verify_certificate_signature(child, issuer)
        top_of_chain = chain[-1]
        trusted = False
        for root in trusted_roots:
            if top_of_chain.tbs_certificate_bytes == root.tbs_certificate_bytes:
                trusted = True
                break
            try:
                _verify_certificate_signature(top_of_chain, root)
                trusted = True
                break
            except Exception:
                continue
        if not trusted:
            raise InvalidReceipt('x5c chain does not terminate at a pinned root')

        leaf_certificate = chain[0]
        signing_input = f'{header_segment}.{payload_segment}'.encode()
        raw_signature = _base64url_decode(signature_segment)
        half = len(raw_signature) // 2
        der_signature = encode_dss_signature(
            int.from_bytes(raw_signature[:half], 'big'),
            int.from_bytes(raw_signature[half:], 'big'))
        leaf_certificate.public_key().verify(
            der_signature, signing_input, ec.ECDSA(hashes.SHA256()))
    except InvalidReceipt:
        raise
    except Exception as verification_error:
        raise InvalidReceipt(f'signature verification failed: {verification_error}')


def _verify_certificate_signature(certificate, issuer):
    """Raise unless `certificate` was signed by `issuer`'s key (EC or RSA)."""
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    issuer_public_key = issuer.public_key()
    if isinstance(issuer_public_key, rsa.RSAPublicKey):
        issuer_public_key.verify(
            certificate.signature, certificate.tbs_certificate_bytes,
            padding.PKCS1v15(), certificate.signature_hash_algorithm)
    elif isinstance(issuer_public_key, ec.EllipticCurvePublicKey):
        issuer_public_key.verify(
            certificate.signature, certificate.tbs_certificate_bytes,
            ec.ECDSA(certificate.signature_hash_algorithm))
    else:
        raise InvalidReceipt('unsupported issuer key type in x5c chain')


def has_active_team_entitlement(device_id):
    """True when the device holds an unexpired team subscription."""
    if not device_id:
        return False
    now = datetime.utcnow()
    for entitlement in Entitlement.objects(device_id=device_id.lower()):
        if TEAM_PRODUCT_FRAGMENT in (entitlement.product_id or ''):
            if entitlement.expires_at and entitlement.expires_at > now:
                return True
    return False


def require_team_entitlement():
    """None when the caller may use team write features; else (response, 402)."""
    if not current_app.config.get('ENTITLEMENTS_ENFORCED', True):
        return None
    principal = current_principal()
    if principal and principal.get('admin'):
        return None
    if has_active_team_entitlement(effective_device_id()):
        return None
    return jsonify({'reason': 'entitlement_required'}), 402


def _team_entitlement_json(device_id):
    now = datetime.utcnow()
    newest_expiry = None
    for entitlement in Entitlement.objects(device_id=device_id):
        if TEAM_PRODUCT_FRAGMENT in (entitlement.product_id or ''):
            if newest_expiry is None or (entitlement.expires_at
                                         and entitlement.expires_at > newest_expiry):
                newest_expiry = entitlement.expires_at
    return {
        'active': bool(newest_expiry and newest_expiry > now),
        'expires_at': newest_expiry.strftime(TIMESTAMP_FORMAT) if newest_expiry else None,
    }


# SUBMIT a StoreKit receipt
@entitlements_blueprint.route('/devices/<identifier>/receipt', methods=['POST'])
@auth.login_required
def submit_receipt(identifier):
    denial = actor_denial(identifier)
    if denial:
        return denial

    json_data = request.get_json(silent=True) or {}
    jws_string = json_data.get('jws')
    if not jws_string:
        return jsonify({'reason': 'jws_required'}), 400

    verifier = current_app.config.get('ENTITLEMENT_VERIFIER') or default_jws_verifier
    try:
        verified = verifier(jws_string)
    except VerifierUnavailable:
        return jsonify({'reason': 'verifier_unavailable'}), 503
    except Exception:
        return jsonify({'reason': 'invalid_receipt'}), 400

    device_id = str(identifier).lower()
    Entitlement.objects(device_id=device_id,
                        product_id=verified['product_id']).update_one(
        set__expires_at=verified['expires_at'],
        set__environment=verified.get('environment'),
        upsert=True)

    return jsonify({'entitlements': {'team': _team_entitlement_json(device_id)}})


# READ entitlements
@entitlements_blueprint.route('/devices/<identifier>/entitlements', methods=['GET'])
@auth.login_required
def get_entitlements(identifier):
    denial = actor_denial(identifier)
    if denial:
        return denial

    return jsonify({'team': _team_entitlement_json(str(identifier).lower())})
