#!/usr/bin/env bash
# Black-box check of a running server using plain curl.
#   uvicorn app.main:app --port 8000     (in another terminal)
#   bash scripts/curl_smoke.sh [base_url]
set -u
BASE="${1:-http://127.0.0.1:8000}/api/v1"
PY="${PYTHON:-python}"
pass=0; fail=0

for _ in $(seq 1 30); do   # wait for the server to come up
  curl -s -o /dev/null --max-time 2 "${BASE%/api/v1}/docs" && break; sleep 1
done

field() { "$PY" -c "import sys,json; d=json.load(sys.stdin); print(d$1)" 2>/dev/null; }

# req METHOD PATH [JSON]  -> sets $CODE and $BODY
req() {
  local out
  if [ $# -ge 3 ]; then
    out=$(curl -s -w $'\n%{http_code}' -X "$1" "$BASE$2" -H 'Content-Type: application/json' -d "$3")
  else
    out=$(curl -s -w $'\n%{http_code}' -X "$1" "$BASE$2")
  fi
  CODE="${out##*$'\n'}"; BODY="${out%$'\n'*}"
}

check() { # label, expected, actual
  if [ "$2" = "$3" ]; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1  (expected '$2', got '$3')"; fail=$((fail+1)); fi
}

new_account() { req POST /accounts "{\"name\":\"$1\"}"; echo "$BODY" | field "['id']"; }
tx() { req POST "/accounts/$1/transactions" "{\"type\":\"$2\",\"amount\":\"$3\",\"reference_id\":\"$4\"}"; }
balance() { req GET "/accounts/$1/balance"; echo "$BODY" | field "['balance']"; }
RUN=$(date +%s)   # keeps reference ids unique across runs

echo "== 1. accounts"
req POST /accounts '{"name":"Alice","balance":500}'
check "create account -> 201" 201 "$CODE"
check "client-sent balance ignored" "0.00" "$(echo "$BODY" | field "['balance']")"
req POST /accounts '{}';                 check "missing name -> 422" 422 "$CODE"
req GET /accounts/does-not-exist/balance; check "unknown account balance -> 404" 404 "$CODE"

echo "== 2. credit / debit / duplicates"
A=$(new_account A)
tx "$A" credit 100 "seed-$RUN"; check "credit -> 201" 201 "$CODE"
tx "$A" credit 50 "c-$RUN";     check "100 + 50 = 150" "150.00" "$(balance "$A")"
tx "$A" debit 30 "d-$RUN";      check "150 - 30 = 120" "120.00" "$(balance "$A")"
tx "$A" debit 150 "big-$RUN";   check "debit more than balance -> 422" 422 "$CODE"
check "rejected debit leaves balance" "120.00" "$(balance "$A")"
tx "$A" credit 100 "seed-$RUN"; check "duplicate reference -> 200" 200 "$CODE"
check "duplicate flag + message" "True" "$(echo "$BODY" | "$PY" -c "import sys,json; d=json.load(sys.stdin); print(d['duplicate'] and 'already exists' in d['message'])")"
check "duplicate leaves balance" "120.00" "$(balance "$A")"
tx "$A" credit 999 "seed-$RUN"; check "same reference, different amount -> 422" 422 "$CODE"
B=$(new_account B)
tx "$B" credit 10 "seed-$RUN";  check "same reference on another account -> 201" 201 "$CODE"
tx "$A" refund 10 "x-$RUN";     check "invalid type -> 422" 422 "$CODE"
tx "$A" credit 0 "x-$RUN";      check "amount 0 -> 422" 422 "$CODE"
tx "$A" credit -5 "x-$RUN";     check "negative amount -> 422" 422 "$CODE"
tx "$A" credit 1.005 "x-$RUN";  check "3 decimal places -> 422" 422 "$CODE"
req POST "/accounts/$A/transactions" '{"type":"credit","amount":"1"}'; check "missing reference_id -> 422" 422 "$CODE"
tx nope credit 1 "x-$RUN";      check "transaction on unknown account -> 404" 404 "$CODE"

echo "== 3. history"
H=$(new_account H)
for i in 1 2 3 4 5; do tx "$H" credit 10 "c$i-$RUN" >/dev/null; done
for i in 1 2;       do tx "$H" debit 1 "d$i-$RUN" >/dev/null; done
req GET "/accounts/$H/transactions?page=2&limit=3"
check "pagination meta" "{'current_page': 2, 'page_size': 3, 'total_records': 7, 'total_pages': 3}" "$(echo "$BODY" | field "['pagination']")"
req GET "/accounts/$H/transactions?type=debit"
check "filter type=debit" 2 "$(echo "$BODY" | field "['pagination']['total_records']")"
req GET "/accounts/$H/transactions"
check "latest first" "d2-$RUN" "$(echo "$BODY" | field "['data'][0]['reference_id']")"
for q in page=0 limit=0 limit=201 type=refund page=abc; do
  req GET "/accounts/$H/transactions?$q"; check "bad query $q -> 422" 422 "$CODE"
done

echo "== 4. integrity"
I=$(new_account I)
for t in "credit 100 1" "credit 50 2" "debit 30 3" "debit 20 4"; do set -- $t; tx "$I" "$1" "$2" "i$3-$RUN" >/dev/null; done
check "0 +100 +50 -30 -20 = 100" "100.00" "$(balance "$I")"
TID=$(curl -s "$BASE/accounts/$I/transactions" | field "['data'][0]['id']")
req PUT "/accounts/$I/transactions/$TID" '{"amount":"1"}'; check "PUT transaction not allowed" "yes" "$([ "$CODE" = 404 ] || [ "$CODE" = 405 ] && echo yes)"
req DELETE "/accounts/$I/transactions/$TID";               check "DELETE transaction not allowed" "yes" "$([ "$CODE" = 404 ] || [ "$CODE" = 405 ] && echo yes)"
req DELETE "/accounts/$I";                                 check "DELETE account -> 405" 405 "$CODE"
req GET /nothing-here; check "unknown route uses the standard error body" "not_found" "$(echo "$BODY" | field "['error']")"

echo "== 5. transfers"
TA=$(new_account TA); TB=$(new_account TB)
tx "$TA" credit 100 "s-$RUN" >/dev/null; tx "$TB" credit 50 "s-$RUN" >/dev/null
req POST /transfers "{\"from_account_id\":\"$TA\",\"to_account_id\":\"$TB\",\"amount\":\"30\",\"reference_id\":\"t1-$RUN\"}"
check "transfer -> 201" 201 "$CODE"
check "A 100 -> 70" "70.00" "$(balance "$TA")"; check "B 50 -> 80" "80.00" "$(balance "$TB")"
req POST /transfers "{\"from_account_id\":\"$TA\",\"to_account_id\":\"$TB\",\"amount\":\"30\",\"reference_id\":\"t1-$RUN\"}"
check "transfer retry -> 200" 200 "$CODE"; check "retry moved nothing" "70.00" "$(balance "$TA")"
req POST /transfers "{\"from_account_id\":\"$TA\",\"to_account_id\":\"$TB\",\"amount\":\"999\",\"reference_id\":\"t2-$RUN\"}"
check "insufficient funds -> 422" 422 "$CODE"
req POST /transfers "{\"from_account_id\":\"$TA\",\"to_account_id\":\"nope\",\"amount\":\"1\",\"reference_id\":\"t3-$RUN\"}"
check "unknown destination -> 404" 404 "$CODE"
req POST /transfers "{\"from_account_id\":\"$TA\",\"to_account_id\":\"$TA\",\"amount\":\"1\",\"reference_id\":\"t4-$RUN\"}"
check "same account -> 422" 422 "$CODE"
check "failed transfers moved nothing" "70.00/80.00" "$(balance "$TA")/$(balance "$TB")"

echo "== 6. concurrency (parallel curls)"
C=$(new_account C)
for i in $(seq 1 15); do
  curl -s -o /dev/null -X POST "$BASE/accounts/$C/transactions" -H 'Content-Type: application/json' \
    -d "{\"type\":\"credit\",\"amount\":\"10\",\"reference_id\":\"same-$RUN\"}" &
done; wait
check "15 parallel identical requests credit once" "10.00" "$(balance "$C")"
check "...and create one row" 1 "$(curl -s "$BASE/accounts/$C/transactions" | field "['pagination']['total_records']")"

D=$(new_account D); tx "$D" credit 100 "seed-$RUN" >/dev/null
for i in $(seq 1 10); do
  curl -s -o /dev/null -X POST "$BASE/accounts/$D/transactions" -H 'Content-Type: application/json' \
    -d "{\"type\":\"debit\",\"amount\":\"30\",\"reference_id\":\"race$i-$RUN\"}" &
done; wait
FINAL=$(balance "$D")
DEBITS=$(curl -s "$BASE/accounts/$D/transactions?type=debit" | field "['pagination']['total_records']")
check "10 parallel debits of 30 from 100: exactly 3 succeed" 3 "$DEBITS"
check "...balance is 10.00, never negative" "10.00" "$FINAL"


code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
post_tx() { code -X POST "$BASE/accounts/$1/transactions" -H 'Content-Type: application/json' -d "$2"; }
post_py() { # post a body built in Python, sent as real UTF-8 (shell argument encoding varies by OS)
  local f; f=$(mktemp)
  "$PY" -c "import json,sys; sys.stdout.buffer.write(json.dumps($2, ensure_ascii=False).encode('utf-8'))" > "$f"
  code -X POST "$BASE/accounts/$1/transactions" -H 'Content-Type: application/json' --data-binary @"$f"; rm -f "$f"
}
rep() { printf "$1%.0s" $(seq 1 "$2"); }   # rep CHAR COUNT

echo "== 7. hostile amounts (all must be 422 and leave the balance alone)"
E=$(new_account E)
for amt in '"NaN"' '"Infinity"' '"-Infinity"' '"1e2"' '"+10"' '" 10 "' '"1,000"' '"1.005"' '"0.001"' \
           '"1000000000000.01"' '"0"' '"-1"' '""' '"abc"' 'true' 'null' '[1]' '{"a":1}' "\"$(rep 9 400)\""; do
  check "amount ${amt:0:30}" 422 "$(post_tx "$E" "{\"type\":\"credit\",\"amount\":$amt,\"reference_id\":\"bad-$RUN\"}")"
done
check "none of them changed the balance" "0.00" "$(balance "$E")"
check "amount '10.000' is fine (still 2 decimal places)" 201 "$(post_tx "$E" "{\"type\":\"credit\",\"amount\":\"10.000\",\"reference_id\":\"ok1-$RUN\"}")"
check "...and was stored as 10.00" "10.00" "$(balance "$E")"
check "rejected requests did not burn the reference" 201 "$(post_tx "$E" "{\"type\":\"credit\",\"amount\":\"1\",\"reference_id\":\"bad-$RUN\"}")"

echo "== 8. hostile strings and shapes"
for ref in '""' '"   "' '"a\u0000b"' '"a\nb"' '"transfer:x"' "\"$(rep r 201)\""; do
  check "reference_id ${ref:0:30}" 422 "$(post_tx "$E" "{\"type\":\"credit\",\"amount\":\"1\",\"reference_id\":$ref}")"
done
check "reference_id with CJK + emoji" 201 "$(post_py "$E" "{'type':'credit','amount':'1','reference_id':chr(0x8BA2)+chr(0x5355)+'-'+chr(0x1F642)+'-$RUN'}")"
check "amount in Arabic-Indic digits" 422 "$(post_py "$E" "{'type':'credit','amount':''.join(chr(0x660+int(c)) for c in '123'),'reference_id':'ar-$RUN'}")"
check "reference_id that looks like SQL" 201 "$(post_tx "$E" "{\"type\":\"credit\",\"amount\":\"1\",\"reference_id\":\"'; DROP TABLE accounts;--$RUN\"}")"
tx "$E" credit 1 "  padded-$RUN  " >/dev/null
tx "$E" credit 1 "padded-$RUN";  check "padded reference replays instead of double-crediting" 200 "$CODE"
check "description 501 chars" 422 "$(post_tx "$E" "{\"type\":\"credit\",\"amount\":\"1\",\"reference_id\":\"d1-$RUN\",\"description\":\"$(rep d 501)\"}")"
check "description null" 201 "$(post_tx "$E" "{\"type\":\"credit\",\"amount\":\"1\",\"reference_id\":\"d2-$RUN\",\"description\":null}")"
check "account name whitespace only" 422 "$(code -X POST "$BASE/accounts" -H 'Content-Type: application/json' -d '{"name":"   "}')"
check "account name with NUL" 422 "$(code -X POST "$BASE/accounts" -H 'Content-Type: application/json' -d '{"name":"a\u0000b"}')"
UNI=$(mktemp)
"$PY" -c "import json,sys; n='Jos'+chr(233)+' '+chr(0x1F642)+' '+chr(0x674E); sys.stdout.buffer.write(json.dumps({'name': n}, ensure_ascii=False).encode('utf-8'))" > "$UNI"
check "account name unicode (sent as UTF-8)" 201 "$(code -X POST "$BASE/accounts" -H 'Content-Type: application/json' --data-binary @"$UNI")"
printf '{"name":"\351\377"}' > "$UNI"   # bytes E9 FF: not valid UTF-8
check "body that is not valid UTF-8 -> 400" 400 "$(code -X POST "$BASE/accounts" -H 'Content-Type: application/json' --data-binary @"$UNI")"
rm -f "$UNI"
check "malformed JSON" 422 "$(code -X POST "$BASE/accounts" -H 'Content-Type: application/json' -d '{')"
check "empty body" 422 "$(code -X POST "$BASE/accounts" -H 'Content-Type: application/json')"
check "text/plain body" 422 "$(code -X POST "$BASE/accounts" -H 'Content-Type: text/plain' -d '{"name":"x"}')"
check "trailing slash is a 404, not a redirect" 404 "$(code -X POST "$BASE/accounts/" -H 'Content-Type: application/json' -d '{"name":"x"}')"
for bad in 'a%00b' "$(rep x 3000)" "1'%20OR%20'1'='1" '..%2F..%2Fetc%2Fpasswd'; do
  check "hostile account id ${bad:0:20}" 404 "$(code "$BASE/accounts/$bad/balance")"
done
check "PUT account -> 405" 405 "$(code -X PUT "$BASE/accounts/$E" -H 'Content-Type: application/json' -d '{"name":"x"}')"
check "PATCH balance -> 405" 405 "$(code -X PATCH "$BASE/accounts/$E/balance" -H 'Content-Type: application/json' -d '{"balance":1}')"
check "DELETE transactions -> 405" 405 "$(code -X DELETE "$BASE/accounts/$E/transactions")"

echo "== 9. pagination limits"
for q in 'page=9223372036854775807' 'page=99999999999999999999' 'page=1000001' 'limit=201' 'limit=0' 'page=1.5' 'type=CREDIT' 'type=%00'; do
  check "history ?$q" 422 "$(code "$BASE/accounts/$E/transactions?$q")"
done
check "history ?page=1000000&limit=200 (largest allowed)" 200 "$(code "$BASE/accounts/$E/transactions?page=1000000&limit=200")"

echo "== 10. transfer edge cases"
X1=$(new_account X1); X2=$(new_account X2); X3=$(new_account X3)
tx "$X1" credit 100 "s-$RUN" >/dev/null
xf() { req POST /transfers "{\"from_account_id\":\"$1\",\"to_account_id\":\"$2\",\"amount\":\"$3\",\"reference_id\":\"$4\"}"; }
xf "$X1" "$X2" 10 "e1-$RUN";  check "transfer ok" 201 "$CODE"
xf "$X1" "$X3" 10 "e1-$RUN";  check "same reference, different destination -> 422 (not 500)" 422 "$CODE"
check "...error code" reference_conflict "$(echo "$BODY" | field "['error']")"
xf "$X1" "$X2" 20 "e1-$RUN";  check "same reference, different amount -> 422" 422 "$CODE"
xf "$X1" "$X2" 10 "e1-$RUN";  check "identical retry -> 200" 200 "$CODE"
check "retry flagged as replay" "True" "$(echo "$BODY" | field "['debit_transaction']['duplicate']")"
tx "$X1" debit 1 "e1-$RUN:debit"; check "ordinary reference 'x:debit' cannot collide with a transfer leg" 201 "$CODE"
xf "$X1" "$X2" 1 "transfer:abc"; check "reserved transfer: prefix -> 422" 422 "$CODE"
xf "$X1" "$X2" 1 "$(rep y 151)"; check "transfer reference over 150 chars -> 422" 422 "$CODE"
for amt in 0 -1 NaN 1e2 1.005; do xf "$X1" "$X2" "$amt" "ba-$amt-$RUN"; check "transfer amount $amt -> 422" 422 "$CODE"; done
xf "$X1" "$(rep x 65)" 1 "long-$RUN"; check "65-char account id -> 422" 422 "$CODE"
check "failed attempts moved nothing" "89.00/10.00/0.00" "$(balance "$X1")/$(balance "$X2")/$(balance "$X3")"

echo "== 11. more concurrency"
P1=$(new_account P1); P2=$(new_account P2); tx "$P1" credit 100 "s-$RUN" >/dev/null
for i in $(seq 1 15); do
  curl -s -o /dev/null -X POST "$BASE/transfers" -H 'Content-Type: application/json' \
    -d "{\"from_account_id\":\"$P1\",\"to_account_id\":\"$P2\",\"amount\":\"30\",\"reference_id\":\"same-$RUN\"}" &
done; wait
check "15 parallel identical transfers move money once" "70.00/30.00" "$(balance "$P1")/$(balance "$P2")"

O1=$(new_account O1); O2=$(new_account O2); tx "$O1" credit 500 "s-$RUN" >/dev/null; tx "$O2" credit 500 "s-$RUN" >/dev/null
OUT=$(mktemp -d)
for i in $(seq 1 20); do
  if [ $((i % 2)) -eq 0 ]; then F=$O1; T=$O2; else F=$O2; T=$O1; fi
  { code -X POST "$BASE/transfers" -H 'Content-Type: application/json' \
      -d "{\"from_account_id\":\"$F\",\"to_account_id\":\"$T\",\"amount\":\"1\",\"reference_id\":\"ab$i-$RUN\"}"; echo; } > "$OUT/$i" &
done; wait
check "20 opposite-direction transfers: none failed (no deadlock)" 20 "$(cat "$OUT"/* | grep -c '^201$')"
check "...money conserved" "1000.00" "$("$PY" -c "from decimal import Decimal as D; print(D('$(balance "$O1")')+D('$(balance "$O2")'))")"
rm -rf "$OUT"

S1=$(new_account S1); tx "$S1" credit 100 "s-$RUN" >/dev/null
OUT=$(mktemp -d); DESTS=""
for i in $(seq 1 10); do D=$(new_account "D$i"); DESTS="$DESTS $D"; done
i=0; for D in $DESTS; do i=$((i+1))
  { code -X POST "$BASE/transfers" -H 'Content-Type: application/json' \
      -d "{\"from_account_id\":\"$S1\",\"to_account_id\":\"$D\",\"amount\":\"30\",\"reference_id\":\"drain$i-$RUN\"}"; echo; } > "$OUT/$i" &
done; wait
check "10 parallel transfers of 30 from 100: exactly 3 succeed" 3 "$(cat "$OUT"/* | grep -c '^201$')"
check "...the rest are 422 insufficient" 7 "$(cat "$OUT"/* | grep -c '^422$')"
check "...source keeps 10.00" "10.00" "$(balance "$S1")"
rm -rf "$OUT"

M=$(new_account M)
for i in $(seq 1 40); do
  curl -s -o /dev/null -X POST "$BASE/accounts/$M/transactions" -H 'Content-Type: application/json' \
    -d "{\"type\":\"credit\",\"amount\":\"7\",\"reference_id\":\"par$i-$RUN\"}" &
done; wait
check "40 parallel credits of 7 lose no update" "280.00" "$(balance "$M")"
check "...and 40 rows exist" 40 "$(curl -s "$BASE/accounts/$M/transactions?limit=200" | field "['pagination']['total_records']")"

echo "== 12. health and Unicode look-alikes"
check "GET /health -> 200" 200 "$(code "${BASE%/api/v1}/health")"
check "POST /health -> 405" 405 "$(code -X POST "${BASE%/api/v1}/health")"
U=$(new_account U)
check "reference 'cafe' with one-character accent" 201 "$(post_py "$U" "{'type':'credit','amount':'1','reference_id':'caf'+chr(0xE9)+'-$RUN'}")"
check "same reference typed as e + combining accent replays" 200 "$(post_py "$U" "{'type':'credit','amount':'1','reference_id':'cafe'+chr(0x301)+'-$RUN'}")"
check "...balance credited once" "1.00" "$(balance "$U")"
check "zero-width space in reference -> 422" 422 "$(post_py "$U" "{'type':'credit','amount':'1','reference_id':'r'+chr(0x200B)+'1-$RUN'}")"
check "byte-order mark in reference -> 422" 422 "$(post_py "$U" "{'type':'credit','amount':'1','reference_id':chr(0xFEFF)+'r-$RUN'}")"
check "right-to-left override in description -> 422" 422 "$(post_py "$U" "{'type':'credit','amount':'1','reference_id':'bidi-$RUN','description':'invoice'+chr(0x202E)+'fdp.exe'}")"
check "full-width digits as amount -> 422" 422 "$(post_py "$U" "{'type':'credit','amount':chr(0xFF11)+chr(0xFF10),'reference_id':'fw-$RUN'}")"
F=$(mktemp)
printf '{"type":"credit","amount":"1","reference_id":"a\\ud800b"}' > "$F"
check "unpaired surrogate in reference -> 422" 422 "$(code -X POST "$BASE/accounts/$U/transactions" -H 'Content-Type: application/json' --data-binary @"$F")"
rm -f "$F"

echo; echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
