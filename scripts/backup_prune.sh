#!/usr/bin/env bash
# Delete a store's backups that are older than the retention period
# (backup/README.md: Retention). Backups are short-lived restore points, not an
# archive: make runs this after every backup that succeeded, through
# scripts/audit-admin.sh, which records it as delete / backup.prune.<store>.
#
#   BACKUP_RETENTION_DAYS=<1..365> BACKUP_DIR=<dir> AUDIT_FACTS=<file> \
#     scripts/backup_prune.sh <neo4j|qdrant> [--now <epoch seconds>]
#   BACKUP_RETENTION_DAYS=<1..365> scripts/backup_prune.sh check
#       exit 0 when the period is valid, 2 otherwise; touches nothing. make runs
#       it before a backup asks admin-audit or a store for anything.
#
# A backup's age is the UTC time in its name, the last one before the suffix:
# neo4j-<YYYYmmddTHHMMSSZ>.dump[.age], qdrant-<collection>-<stamp>[-n].snapshot[.age].
# Encrypted backups and the plaintext ones of earlier versions follow the same
# rule. Never deleted, only named: unfinished files (.part), names without a
# stamp or that the audit record cannot carry (only letters, digits and ._-),
# and stamps in the future (a clock set back).
#
# A period that is new on this host takes effect when a run first sees it and
# deletes nothing for the notice period after that (RETENTION_NOTICE_DAYS, 1 to
# 365, default 30), so nothing an earlier version left is deleted without
# notice. A changed period never postpones what the period in force already
# made due: a longer one applies at once, and a shorter one gives the notice
# period only to the backups it makes due sooner. Changing the notice alone
# moves nothing. The state is $BACKUP_DIR/.prune-since, "<since> <days>
# <grace until> [<days in force> <their grace until>]" in epoch seconds, mode
# 0644 so every account that backs up can read it: while the grace runs, a
# backup goes when the rule in force before the change (its days and its own
# grace) would delete it. The two-field form of earlier versions is read as a
# grace that started at <since>. A file this account
# cannot delete is named and counted as not deleted; it does not fail the run.
#
# Reports the facts of the record to $AUDIT_FACTS: period_days, cutoff (the
# effective one), deleted, not_deleted, file=<name> for each file deleted, and
# while a grace runs grace_until and, after a change, in_force_days. Exits 0
# when it ran, 2 on a bad setting, argument or state file. --now is for tests.
set -euo pipefail

usage() {
  echo "usage: BACKUP_RETENTION_DAYS=<1..365> BACKUP_DIR=<dir> $0 <neo4j|qdrant> [--now <epoch>] | $0 check" >&2
  exit 2
}

# shellcheck source=scripts/backup_age.sh
. "$(dirname "${BASH_SOURCE[0]}")/backup_age.sh"

[[ $# -eq 1 || $# -eq 3 ]] || usage
store=$1
case $store:$# in neo4j:* | qdrant:* | check:1) ;; *) usage ;; esac
now=$(date -u +%s)
if [[ $# -eq 3 ]]; then
  [[ $2 == --now && $3 =~ ^[0-9]+$ ]] || usage
  now=$3
fi

days=${BACKUP_RETENTION_DAYS:-}
if [[ ! $days =~ ^[0-9]{1,3}$ ]] || ((10#$days < 1 || 10#$days > 365)); then
  echo "ERROR: BACKUP_RETENTION_DAYS must be whole days from 1 to 365, not '$days'. Nothing was deleted." >&2
  exit 2
fi
days=$((10#$days))
notice=${RETENTION_NOTICE_DAYS:-30}
if [[ ! $notice =~ ^[0-9]{1,3}$ ]] || ((10#$notice < 1 || 10#$notice > 365)); then
  echo "ERROR: RETENTION_NOTICE_DAYS must be whole days from 1 to 365, not '$notice'. Nothing was deleted." >&2
  exit 2
fi
notice=$((10#$notice))
[[ $store != check ]] || exit 0
facts=${AUDIT_FACTS:-/dev/null}
period=$((days * 86400))

# _epoch YYYYmmddTHHMMSSZ: seconds since the epoch, computed in bash, so GNU
# and BSD date behave the same (days from the civil calendar).
_epoch() {
  local s=$1 y m d hh mm ss era yoe doy doe
  y=$((10#${s:0:4})) m=$((10#${s:4:2})) d=$((10#${s:6:2}))
  hh=$((10#${s:9:2})) mm=$((10#${s:11:2})) ss=$((10#${s:13:2}))
  ((m >= 1 && m <= 12 && d >= 1 && d <= 31 && hh <= 23 && mm <= 59 && ss <= 59)) || return 1
  ((m <= 2)) && y=$((y - 1))
  era=$((y / 400)) yoe=$((y - y / 400 * 400))
  doy=$(((153 * (m > 2 ? m - 3 : m + 9) + 2) / 5 + d - 1))
  doe=$((yoe * 365 + yoe / 4 - yoe / 100 + doy))
  echo $(((era * 146097 + doe - 719468) * 86400 + hh * 3600 + mm * 60 + ss))
}

# _utc EPOCH: YYYY-mm-ddTHH:MM:SSZ (GNU date, then BSD).
_utc() {
  date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ
}

dir=$(backup_dir)
state="$dir/.prune-since"

# _record_state SINCE GRACE_UNTIL [IN_FORCE_DAYS IN_FORCE_GRACE]: write the
# state (a per-run temporary name, so two runs at once never fail each
# other's write).
_record_state() {
  if ! (umask 022 && printf '%s %s %s%s\n' "$1" "$days" "$2" "${3:+ $3 $4}" >"$state.tmp.$$" &&
    mv -f "$state.tmp.$$" "$state"); then
    rm -f "$state.tmp.$$"
    echo "ERROR: cannot write $state with this account. Nothing was deleted." >&2
    exit 2
  fi
}
if [[ ! -e $state ]]; then
  _record_state "$now" $((now + notice * 86400))
fi
if [[ ! -r $state ]]; then
  echo "ERROR: this account cannot read $state: make it readable (chmod 644), or run the backups as its owner. Nothing was deleted." >&2
  exit 2
fi
since='' recorded='' grace='' inforce='' inforce_grace=''
read -r since recorded grace inforce inforce_grace _ <"$state" || true
if [[ ! $since =~ ^[0-9]+$ || ! ${recorded:-} =~ ^[0-9]+$ ]] ||
  [[ -n $grace && ! $grace =~ ^[0-9]+$ ]] || [[ -n $inforce && ! $inforce =~ ^[0-9]+$ ]] ||
  [[ -n $inforce && ! ${inforce_grace:-} =~ ^[0-9]+$ ]]; then
  echo "ERROR: $state holds no time and period. Delete it to restart the grace period. Nothing was deleted." >&2
  exit 2
fi
since=$((10#$since)) recorded=$((10#$recorded))
[[ -n $grace ]] && grace=$((10#$grace)) || grace=$((since + notice * 86400))
[[ -z $inforce ]] || inforce=$((10#$inforce)) inforce_grace=$((10#$inforce_grace))
if ((since > now)); then
  echo "WARNING: $state is dated in the future; the grace period starts again now. Check the host's clock." >&2
  since=$now grace=$((now + notice * 86400)) inforce='' inforce_grace=''
  _record_state "$since" "$grace"
elif ((recorded != days)); then
  # The rule in force meanwhile: the old period with its own grace, unless
  # that grace had not passed and an older rule was still in force in it.
  if ((now >= grace)) || [[ -z $inforce ]]; then
    inforce=$recorded inforce_grace=$grace
  fi
  echo ">> BACKUP_RETENTION_DAYS changed from $recorded to $days: the new period takes effect now" >&2
  since=$now grace=$((now + notice * 86400))
  _record_state "$since" "$grace" "$inforce" "$inforce_grace"
fi

deleted=0
not_deleted=0
if ((now >= grace)); then
  cutoff=$((now - period))
elif [[ -n $inforce ]] && ((now >= inforce_grace)); then
  # During the grace only what both the new period and the rule in force
  # make due goes: nothing sooner than announced, nothing later either.
  cutoff=$((now - (inforce > days ? inforce : days) * 86400))
  echo ">> until $(_utc "$grace") only backups older than $((inforce > days ? inforce : days)) day(s) are deleted" >&2
else
  cutoff=$((now - period))
  echo ">> pruning takes effect on $(_utc "$grace"): until then no backup is deleted" >&2
  printf 'period_days=%s\ncutoff=%s\ngrace_until=%s\ndeleted=0\nnot_deleted=0\n' \
    "$days" "$(_utc "$cutoff")" "$(_utc "$grace")" >>"$facts"
  exit 0
fi
printf 'period_days=%s\ncutoff=%s\n' "$days" "$(_utc "$cutoff")" >>"$facts"
if ((now < grace)); then
  printf 'grace_until=%s\nin_force_days=%s\n' "$(_utc "$grace")" "$inforce" >>"$facts"
fi

if [[ $store == neo4j ]]; then
  pattern='^neo4j-([0-9]{8}T[0-9]{6}Z)\.dump(\.age)?$'
else
  pattern='^qdrant-[A-Za-z0-9._-]+-([0-9]{8}T[0-9]{6}Z)(-[0-9]+)?\.snapshot(\.age)?$'
fi
shopt -s nullglob
for path in "$dir/$store"-*; do
  name=${path##*/}
  [[ -f $path && ! -L $path ]] || continue
  if [[ $name == *.part ]]; then
    echo ">> left alone: $name (an unfinished backup)" >&2
    continue
  fi
  if ((${#name} > 255)) || [[ ! $name =~ $pattern ]] || ! written=$(_epoch "${BASH_REMATCH[1]}"); then
    echo ">> left alone: $name (no time in its name, or a name the record cannot carry)" >&2
    continue
  fi
  if ((written > now)); then
    echo "WARNING: $name is stamped in the future; kept. Check the host's clock." >&2
    continue
  fi
  ((written < cutoff)) || continue
  # Without -f, so a file another pruning removed first is not counted here;
  # stdin is not a terminal, so rm never asks about a write-protected file.
  # shellcheck disable=SC2217
  if rm -- "$path" </dev/null 2>/dev/null; then
    printf 'file=%s\n' "$name" >>"$facts"
    deleted=$((deleted + 1))
  elif [[ -e $path ]]; then
    echo "ERROR: cannot delete $name with this account: remove $name as root." >&2
    not_deleted=$((not_deleted + 1))
  else
    echo ">> already gone: $name (deleted by another pruning)" >&2
  fi
done
printf 'deleted=%s\nnot_deleted=%s\n' "$deleted" "$not_deleted" >>"$facts"
echo ">> pruned $deleted backup(s) of $store older than $days day(s)" >&2
