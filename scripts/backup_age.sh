#!/usr/bin/env bash
# shellcheck disable=SC2034  # AGE_RECIPIENTS is read by the scripts that source this file
# The encryption of the backups with age (backup/README.md: Encryption).
#
# Every backup is encrypted on its way out of the store, to the public keys in
# the recipients file BACKUP_RECIPIENTS (make's default: backup/recipients.txt):
# one public key per line, # comments allowed. Only public keys belong on this
# host; the identity that decrypts stays offline. Plaintext never reaches a
# file on the host: the snapshots stream through age.
#
# The recipients file decides who can read every backup, so only root and the
# account running the backup may be able to change it: the file and every
# directory above it, up to this checkout (or up to / for a file outside it),
# must belong to one of them and be writable by its owner only, except for a
# directory with the sticky bit, such as /tmp. A check that cannot read a mode
# or an owner refuses. The keys are then read once, into a private copy in
# TMPDIR (whose directories are checked the same way) that the count, the
# fingerprint and age all use, so the keys recorded are the keys encrypted to.
#
#   BACKUP_RECIPIENTS=<file> scripts/backup_age.sh recipients
#       exit 0 when backups can be encrypted: the file passes those checks,
#       age is installed and accepts every line of it as a public key.
#   IDENTITY=<file> scripts/backup_age.sh identity <backup file>
#       exit 0 when a restore can decrypt the file: an identity file is given
#       for this run, age is installed, and the file is age-encrypted.
#
# Both exit 2 otherwise, saying what to do. make runs them before a backup or
# a restore asks admin-audit (or the operator) for anything.
#
# scripts/qdrant_admin.sh sources this file for the same checks
# (age_recipients, age_identity), for the facts of the audit record
# (age_facts: the encryption, how many distinct public keys, and the SHA-256
# of that set, never a key) and for the backup directory.

# Set by age_recipients: the private copy of the recipients that age is to
# encrypt to, how many distinct public keys it holds, and the SHA-256 of their
# set.
AGE_RECIPIENTS=""
AGE_RECIPIENT_COUNT=0
AGE_RECIPIENTS_SHA256=""
# The private copy to remove on exit (age_cleanup), and this checkout,
# physically (symbolic links resolved), where the walk up from the recipients
# file stops.
_AGE_COPY=""
_AGE_CHECKOUT=$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)

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

# _age_me: the account running this, for messages.
_age_me() {
  id -un 2>/dev/null || id -u
}

# _age_stat PATH: "<octal mode> <owner uid>" of PATH itself (a symbolic link is
# not followed), the mode with its sticky bit: GNU stat first, then BSD, whose
# %p carries the file type in front of the four mode digits. Returns 1 when
# neither answers in that shape, so that every check built on it fails closed.
_age_stat() {
  local out mode shape='^[0-7]{1,4} [0-9]+$'
  if ! out=$(stat -c '%a %u' "$1" 2>/dev/null); then
    out=$(stat -f '%p %u' "$1" 2>/dev/null) || return 1
    mode=${out% *}
    [[ $mode =~ ^[0-7]{4,}$ ]] || return 1
    out="${mode: -4} ${out#* }"
  fi
  [[ $out =~ $shape ]] || return 1
  printf '%s\n' "$out"
}

# _age_guarded PATH WHAT: check that only root or the account running this can
# change PATH: it belongs to one of them and neither its group nor others may
# write it, unless it is a directory with the sticky bit. Returns 2, saying so,
# when not, or when its mode or owner cannot be read. WHAT names it in the
# messages.
_age_guarded() {
  local info mode owner
  if ! info=$(_age_stat "$1"); then
    echo "ERROR: cannot read the mode and owner of $2, so who can change the recipients is unknown. Nothing was done." >&2
    return 2
  fi
  mode=${info% *}
  owner=${info#* }
  if [[ $owner != 0 && $owner != "$(id -u)" ]]; then
    echo "ERROR: $2 belongs to uid $owner, not to root or $(_age_me), who runs this: its owner chooses who can read the backups." >&2
    echo "       Make root or $(_age_me) its owner. Nothing was done." >&2
    return 2
  fi
  if (((8#$mode & 8#022) != 0)); then
    # A directory others may write, such as /tmp, is safe when its sticky bit
    # is set: nobody can then rename or remove an entry they do not own, and
    # the entries below it are checked on their own.
    if [[ -d $1 ]] && (((8#$mode & 8#1000) != 0)); then
      return 0
    fi
    echo "ERROR: others than its owner can change $2, and with it who can read the backups: chmod go-w it. Nothing was done." >&2
    return 2
  fi
}

# _age_guarded_dirs DIR WHAT: _age_guarded for DIR and every directory above
# it, up to this checkout, or up to / for a directory outside it: whoever can
# change one of them can put another file in place. WHAT names that file.
_age_guarded_dirs() {
  local dir=$1
  while :; do
    _age_guarded "$dir" "the directory $dir (above $2)" || return 2
    if [[ $dir == "$_AGE_CHECKOUT" || $dir == / ]]; then
      return 0
    fi
    dir=$(dirname "$dir")
  done
}

# age_cleanup: remove the private copy of the recipients, if there is one. The
# scripts that source this file call it on every exit.
age_cleanup() {
  if [[ -n $_AGE_COPY ]]; then
    rm -f "$_AGE_COPY"
    _AGE_COPY=""
  fi
}

# age_recipients: check that backups can be encrypted to the public keys in
# $BACKUP_RECIPIENTS, copy them into a private file (removed by age_cleanup),
# and set AGE_RECIPIENTS to that copy, AGE_RECIPIENT_COUNT and
# AGE_RECIPIENTS_SHA256 from it. Returns 2, with what to do on stderr, when
# they cannot be.
age_recipients() {
  local file=${BACKUP_RECIPIENTS:-} dir path tmp copy said
  if [[ -z $file ]]; then
    echo "ERROR: BACKUP_RECIPIENTS names no recipients file, and a backup is never written unencrypted." >&2
    _age_setup_help
    return 2
  fi
  if [[ -L $file ]]; then
    echo "ERROR: $file is a symbolic link: name the recipients file itself. Nothing was done." >&2
    return 2
  fi
  if [[ ! -f $file || ! -r $file ]]; then
    echo "ERROR: there is no readable recipients file at $file, and a backup is never written unencrypted." >&2
    _age_setup_help
    return 2
  fi
  if ! dir=$(cd -P "$(dirname "$file")" 2>/dev/null && pwd -P); then
    echo "ERROR: cannot find the directory of $file. Nothing was done." >&2
    return 2
  fi
  path="$dir/${file##*/}"
  _age_guarded "$path" "$file" || return 2
  _age_guarded_dirs "$dir" "the recipients file" || return 2
  if ! command -v age >/dev/null 2>&1; then
    echo "ERROR: age is not installed on this host (backup/README.md: Installing age). Nothing was done." >&2
    return 2
  fi
  # From here on only the copy is read: what is checked, counted and
  # fingerprinted is what age encrypts to, whatever happens to the file.
  if ! tmp=$(cd -P "${TMPDIR:-/tmp}" 2>/dev/null && pwd -P); then
    echo "ERROR: cannot find the temporary directory ${TMPDIR:-/tmp} (TMPDIR). Nothing was done." >&2
    return 2
  fi
  _age_guarded_dirs "$tmp" "the private copy of the recipients (TMPDIR)" || return 2
  if ! copy=$(mktemp "$tmp/data-plane-recipients.XXXXXX"); then
    echo "ERROR: cannot make a private copy of $file. Nothing was done." >&2
    return 2
  fi
  _AGE_COPY=$copy
  if ! cat "$path" >"$copy"; then
    age_cleanup
    echo "ERROR: cannot read $file. Nothing was done." >&2
    return 2
  fi
  if grep -qE '^(AGE-SECRET-KEY-|AGE-PLUGIN-)|PRIVATE KEY-----' "$copy"; then
    age_cleanup
    echo "ERROR: $file holds a private key. Only public keys belong on this host; the identity stays offline." >&2
    echo "       Remove it from this host, and treat that key as exposed." >&2
    return 2
  fi
  # An empty input encrypted to nowhere: age reads every line. It skips an SSH
  # key it cannot use with only a warning, which would make the count wrong, so
  # any message refuses.
  if ! said=$(age -R "$copy" -o /dev/null </dev/null 2>&1) || [[ -n $said ]]; then
    age_cleanup
    echo "ERROR: age does not take every line of $file as a public key:" >&2
    # Unquoted as a whole, so that bash 3.2 and 5.2 alike take $file literally.
    said=${said//"$copy"/"$file"}
    printf '%s\n' "$said" | sed 's/^/  /' >&2
    return 2
  fi
  AGE_RECIPIENTS=$copy
  AGE_RECIPIENT_COUNT=$(age_recipient_lines "$copy" | wc -l | tr -d ' ')
  AGE_RECIPIENTS_SHA256=$(age_recipient_lines "$copy" | _age_sha256)
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

# warn_plaintext DIR: warn about snapshots an earlier version left unencrypted in DIR.
warn_plaintext() {
  local file count=0
  for file in "$1"/*.snapshot; do
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
  trap age_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  case "${1:-}:$#" in
    recipients:1) age_recipients ;;
    identity:2) age_identity "$2" ;;
    *)
      echo "usage: BACKUP_RECIPIENTS=<file> $0 recipients | IDENTITY=<file> $0 identity <backup file>" >&2
      exit 2
      ;;
  esac
fi
