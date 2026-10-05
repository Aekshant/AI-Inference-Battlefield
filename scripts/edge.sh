#!/usr/bin/env bash
# POST a chaos call to every edge pod:  edge.sh /chaos/partition '{"fraction":0.4,"duration_s":60}'
set -euo pipefail
path=$1; body=${2:-'{}'}
for p in $(kubectl -n battlefield get pods -l app=edge -o name); do
  kubectl -n battlefield exec "$p" -- python -c "
import sys, urllib.request as u
r = u.Request('http://127.0.0.1:8000' + sys.argv[1], data=sys.argv[2].encode(), method='POST',
              headers={'Content-Type': 'application/json'})
print('$p', u.urlopen(r).read().decode()[:160])" "$path" "$body"
done
