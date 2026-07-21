"""Hashing de contrasenas -- punto unico de entrada para todo el proyecto
(scripts de bootstrap hoy, UsuarioService/auth mas adelante). Usa pwdlib
(Argon2) en vez de passlib: passlib no tiene release desde 2020, depende del
modulo `crypt` que se elimina en Python 3.13, y ya no es compatible con
bcrypt>=4.1 (ver docs/ARCHITECTURE_REVIEW.md)."""
from pwdlib import PasswordHash

password_hasher = PasswordHash.recommended()
