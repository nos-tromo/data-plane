#!/usr/bin/env bash
# shellcheck disable=SC2034  # AGE_RECIPIENTS is read by the scripts that source this file
# The encryption of the backups with age (backup/README.md: Encryption).
#
# Every backup is encrypted on its way out of the store, to the public keys in
# the recipients file BACKUP_RECIPIENTS (make's default: backup/recipients.txt):
# one public key per line, # comments allowed. Only public keys belong on this
# host; the identity that decrypts stays offline. Plaintext never reaches a
# file on the host: the dumps and snapshots stream through age.
#
#   BACKUP_RECIPIENTS=<file> scripts/backup_age.sh recipients
#       exit 0 when backups can be encrypted: age is installed and accepts
#       every line of the file as a public key.
#   IDENTITY=<file> scripts/backup_age.sh identity <backup file>
#       exit 0 when a restore can decrypt the file: an identity file is given
#       for this run, age is installed, and the file is age-encrypted.
#
# Both exit 2 otherwise, saying what to do. make runs them before a backup or
# a restore asks admin-audit (or the operator) for anything.
#
# scripts/neo4j_admin.sh and scripts/qdrant_admin.sh source this file for the
# same checks (age_recipients, age_identity), for the facts of the audit
# record (age_facts: the encryption, how many distinct public keys, and the
# SHA-256 of that set, never a key) and for the backup directory.

# Set by age_recipients: the recipients file, how many distinct public keys it
# holds, and the SHA-256 of their set.
AGE_RECIPIENTS=""
AGE_RECIPIENT_COUNT=0
AGE_RECIPIENTS_SHA256=""

# _age_setup_help: how to set up the encryption, after a refusal.
_age_setup_help() {
  cat >&2 <<'EOF'
Backups are encrypted with age to public keys only. Create a key pair OFFLINE,
on a machine other than this host:
  age-keygen -o data-plane-backup.key   # the identity: it stays offline, e.g. on removable media in a safe
  age-keygen -y data-plane-backup.key   # prints its public key, age1...
Put each keyholder's public key on a line of its own into the recipients file
(BACKUP_RECIPIENTS; make's default is backup/recipients.txt). Nothing was done.
backup/README.md (Encryption) has the details.
EOF
}

# age_recipient_lines FILE: the public keys in FILE as age reads them (without
# empty lines, lines starting with #, and a line's trailing CR), sorted bytewise
# and without repeats.
age_recipient_lines() {
  tr -d '\r' <"$1" | { grep -vE '^(#|$)' || true; } | LC_ALL=C sort -u
}

# _age_sha256: the SHA-256 of stdin, in hex.
_age_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum
  else
    shasum -a 256
  fi | cut -d' ' -f1
}

# age_recipients: check that backups can be encrypted to the public keys in
# $BACKUP_RECIPIENTS and set AGE_RECIPIENTS, AGE_RECIPIENT_COUNT and
# AGE_RECIPIENTS_SHA256. Returns 2, with what to do on stderr, when they cannot.
age_recipients() {
  local file=${BACKUP_RECIPIENTS:-} mode said
  if [[ -z $file ]]; then
    echo "ERROR: BACKUP_RECIPIENTS names no recipients file, and a backup is never written unencrypted." >&2
    _age_setup_help
    return 2
  fi
  if [[ ! -f $file || ! -r $file ]]; then
    echo "ERROR: there is no readable recipients file at $file, and a backup is never written unencrypted." >&2
    _age_setup_help
    return 2
  fi
  if grep -qE '^(AGE-SECRET-KEY-|AGE-PLUGIN-)|PRIVATE KEY-----' "$file"; then
    echo "ERROR: $file holds a private key. Only public keys belong on this host; the identity stays offline." >&2
    echo "       Remove it from this host, and treat that key as exposed." >&2
    return 2
  fi
  # Whoever can change the file chooses who can read the backups written next.
  mode=$(stat -c %a "$file" 2>/dev/null || stat -f %Lp "$file" 2>/dev/null) || mode=""
  if [[ $mode =~ ^[0-7]+$ ]] && (((8#$mode & 8#002) != 0)); then
    echo "ERROR: anyone on this host can change $file, and with it who can read the backups: chmod o-w $file" >&2
    return 2
  fi
  if ! command -v age >/dev/null 2>&1; then
    echo "ERROR: age is not installed on this host (backup/README.md: Installing age). Nothing was done." >&2
    return 2
  fi
  # An empty input encrypted to nowhere: age reads every line. It skips an SSH
  # key it cannot use with only a warning, which would make the count wrong, so
  # any message refuses.
  if ! said=$(age -R "$file" -o /dev/null </dev/null 2>&1) || [[ -n $said ]]; then
    echo "ERROR: age does not take every line of $file as a public key:" >&2
    printf '%s\n' "$said" | sed 's/^/  /' >&2
    return 2
  fi
  AGE_RECIPIENTS=$file
  AGE_RECIPIENT_COUNT=$(age_recipient_lines "$file" | wc -l | tr -d ' ')
  AGE_RECIPIENTS_SHA256=$(age_recipient_lines "$file" | _age_sha256)
}

# age_identity FILE: check that a restore can decrypt FILE: IDENTITY names a
# readable identity file, given for this run only, age is installed, and
# FILE is an age-encrypted file. Returns 2, saying why, when it cannot.
age_identity() {
  local file=$1 first=""
  if [[ -z ${IDENTITY:-} ]]; then
    echo "ERROR: a restore decrypts the backup with the age identity it was encrypted to." >&2
    echo "       Give that identity's file for this run only: IDENTITY=<file> (backup/README.md, Restore). Nothing was done." >&2
    return 2
  fi
  if [[ ! -f $IDENTITY || ! -r $IDENTITY ]]; then
    echo "ERROR: there is no readable identity file at $IDENTITY. Nothing was done." >&2
    return 2
  fi
  if ! command -v age >/dev/null 2>&1; then
    echo "ERROR: age is not installed on this host (backup/README.md: Installing age). Nothing was done." >&2
    return 2
  fi
  if [[ ! -f $file || ! -r $file ]]; then
    echo "ERROR: there is no readable backup file at $file. Nothing was done." >&2
    return 2
  fi
  # The first line of an age file names the format (binary, or armored).
  LC_ALL=C IFS= read -r -n 64 first <"$file" || true
  first=${first%$'\r'}
  if [[ $first != "age-encryption.org/v1" && $first != "-----BEGIN AGE ENCRYPTED FILE-----" ]]; then
    echo "ERROR: ${file##*/} is not age-encrypted, and a restore takes only encrypted backups" >&2
    echo "       (backup/README.md: Plaintext from earlier versions). Nothing was done." >&2
    return 2
  fi
}

# age_facts FILE: the encryption facts of a backup's audit record, as
# key=value lines appended to FILE.
age_facts() {
  printf 'encryption=age\nrecipients=%s\nrecipients_sha256=%s\n' "$AGE_RECIPIENT_COUNT" "$AGE_RECIPIENTS_SHA256" >>"$1"
}

# backup_dir: the absolute backup directory (BACKUP_DIR), created on demand.
backup_dir() {
  local dir=${BACKUP_DIR:?BACKUP_DIR must name the backup directory}
  mkdir -p "$dir"
  (cd "$dir" && pwd)
}

# file_size FILE: its size in bytes. stat needs no read permission on the file;
# GNU stat first, then BSD.
file_size() {
  stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null
}

# warn_plaintext DIR: warn about backups an earlier version left unencrypted in DIR.
warn_plaintext() {
  local file count=0
  for file in "$1"/*.dump "$1"/*.snapshot; do
    if [[ -e $file ]]; then
      count=$((count + 1))
    fi
  done
  if [[ $count -gt 0 ]]; then
    echo "WARNING: $count unencrypted backup file(s) in ${BACKUP_DIR:-$1}, written before backups were encrypted:" >&2
    echo "         encrypt or delete them (backup/README.md: Plaintext from earlier versions)." >&2
  fi
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  set -euo pipefail
  case "${1:-}:$#" in
    recipients:1) age_recipients ;;
    identity:2) age_identity "$2" ;;
    *)
      echo "usage: BACKUP_RECIPIENTS=<file> $0 recipients | IDENTITY=<file> $0 identity <backup file>" >&2
      exit 2
      ;;
  esac
fi
