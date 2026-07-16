"""Auth-adjacent infrastructure adapters (password hashing).

JWT lives in ``application.services.jwt_service`` because it's a pure
encode/decode wrapper around a config string with no I/O — application
layer is correct. Password hashing pulls in passlib + bcrypt which is
genuinely infrastructure, so it lives here.
"""
