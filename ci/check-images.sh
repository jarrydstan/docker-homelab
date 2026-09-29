#!/bin/sh
# Fail if any image reference in $1 (one per line) cannot be resolved for linux/amd64.
set -u
fail=0
while read -r img; do
  [ -n "$img" ] || continue
  ref=$img
  case $ref in
    *@sha256:*) ref="${ref%%:*}@${ref#*@}" ;; # skopeo wants name@digest, not name:tag@digest
  esac
  case $ref in
    */*)
      case ${ref%%/*} in
        *.*|*:*|localhost) ;;         # already has a registry host
        *) ref="docker.io/$ref" ;;
      esac ;;
    *) ref="docker.io/library/$ref" ;; # official image, e.g. postgres:16
  esac
  if skopeo inspect --override-os linux --override-arch amd64 --no-tags "docker://$ref" >/dev/null; then
    echo "ok      $img"
  else
    echo "MISSING $img"
    fail=1
  fi
done < "$1"
exit $fail
