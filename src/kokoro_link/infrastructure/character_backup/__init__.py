"""Inner container for ``.lumebackup`` archives (CB series).

Byte-level zip read/write only — the sibling of
``infrastructure.character_card.packager`` for full character backups.
Encryption lives in ``infrastructure.security.backup_cipher``; DTO
projection and id remapping live in the application layer.
"""
