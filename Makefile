# TAZAMA — every recipe delegates to scripts/mk.py so that make, make.ps1 and
# `python scripts/mk.py` cannot drift apart (GNU make is absent on many Windows boxes).

PY ?= $(shell command -v python 2>/dev/null || command -v python3 2>/dev/null || echo py)
MK := $(PY) scripts/mk.py

.DEFAULT_GOAL := help
.PHONY: help bootstrap bootstrap-ml web-install fmt lint typecheck boundaries secrets audit \
        sbom test test-unit test-it coverage-gate eval eval-live e2e a11y load migrate downgrade \
        seed catalog pipeline replay train openapi doctor api worker fake-llm web \
        up up-oracle down \
        clean demo ci

help:          ; @$(MK) help
bootstrap:     ; @$(MK) bootstrap
bootstrap-ml:  ; @$(MK) bootstrap-ml
web-install:   ; @$(MK) web-install
fmt:           ; @$(MK) fmt
lint:          ; @$(MK) lint
typecheck:     ; @$(MK) typecheck
boundaries:    ; @$(MK) boundaries
secrets:       ; @$(MK) secrets
audit:         ; @$(MK) audit
sbom:          ; @$(MK) sbom
test:          ; @$(MK) test
test-unit:     ; @$(MK) test-unit
test-it:       ; @$(MK) test-it
coverage-gate: ; @$(MK) coverage-gate
eval:          ; @$(MK) eval
eval-live:     ; @$(MK) eval-live
e2e:           ; @$(MK) e2e
a11y:          ; @$(MK) a11y
load:          ; @$(MK) load
migrate:       ; @$(MK) migrate
downgrade:     ; @$(MK) downgrade
seed:          ; @$(MK) seed
catalog:       ; @$(MK) catalog
pipeline:      ; @$(MK) pipeline
replay:        ; @$(MK) replay
train:         ; @$(MK) train
openapi:       ; @$(MK) openapi
doctor:        ; @$(MK) doctor
api:           ; @$(MK) api
worker:        ; @$(MK) worker
fake-llm:      ; @$(MK) fake-llm
web:           ; @$(MK) web
up:            ; @$(MK) up
up-oracle:     ; @$(MK) up-oracle
down:          ; @$(MK) down
clean:         ; @$(MK) clean
demo:          ; @$(MK) demo
ci:            ; @$(MK) ci
