# Dev conveniences — see docs/CONTRIBUTING.md "Emulator stack" (#21).
COMPOSE ?= docker compose -f compose.yaml
PYTEST ?= .venv/bin/pytest
REDFISH_URL ?= http://127.0.0.1:8000
KVMD_DIR ?= .kvmd-testenv
KVMD_REF ?= master

.PHONY: help emulators emulators-down emulators-logs integration kvmd-testenv
help:
	@echo "make emulators       # start the emulator stack, detached (Redfish: $(REDFISH_URL))"
	@echo "make integration     # run tests/integration against the running stack"
	@echo "make emulators-down  # stop the stack"
	@echo "make emulators-logs  # follow stack logs"
	@echo "make kvmd-testenv    # run the real kvmd daemon (Linux only; see docs/CONTRIBUTING.md)"
emulators:
	$(COMPOSE) up --build --wait
	@echo "Redfish (sushy-tools --fake): $(REDFISH_URL)/redfish/v1/"
emulators-down:
	$(COMPOSE) down --remove-orphans
emulators-logs:
	$(COMPOSE) logs -f
integration:
	KVM_PILOT_REDFISH_URL=$(REDFISH_URL) $(PYTEST) tests/integration -m integration -v
# Genuine kvmd (PiKVM) daemon via upstream's own recipe. Hard host requirements
# (gpio_mockup module, /dev/video0, debugfs) make this Linux-only; upstream's
# `make run` exits 1 without them. Functional test coverage: #16.
kvmd-testenv:
	@if [ "$$(uname -s)" != "Linux" ]; then \
	  echo "kvmd-testenv needs a Linux host (gpio_mockup + /dev/video0); see docs/CONTRIBUTING.md"; exit 1; fi
	test -d $(KVMD_DIR) || git clone --depth 1 --branch $(KVMD_REF) https://github.com/pikvm/kvmd $(KVMD_DIR)
	$(MAKE) -C $(KVMD_DIR) run
