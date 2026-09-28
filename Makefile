PYTHON ?= python3
APP_ID := io.github.wakedog.SystemUpdate
VERSION := $(shell $(PYTHON) -c 'import system_update; print(system_update.__version__)')
DEB := dist/system-update_$(VERSION)_all.deb

.PHONY: help run test check deb install uninstall clean

help:
	@echo "make run        start the desktop app from this folder"
	@echo "make test       run the unit tests"
	@echo "make check      tests plus desktop, AppStream and polkit file validation"
	@echo "make deb        build $(DEB)"
	@echo "make install    build and install the package (asks for your password)"
	@echo "make uninstall  remove the installed package"

run:
	$(PYTHON) -m system_update

test:
	$(PYTHON) -m unittest discover -s tests -t .

check: test
	desktop-file-validate data/$(APP_ID).desktop
	@out=$$(appstreamcli validate --no-net --no-color data/$(APP_ID).metainfo.xml); \
		echo "$$out" | grep -E '^[EW]:' || echo "AppStream metadata: OK"; \
		! echo "$$out" | grep -q '^E:'
	$(PYTHON) -c "import xml.dom.minidom as m; m.parse('data/$(APP_ID).policy')"
	bash -n data/system-update.bash-completion

deb:
	packaging/build-deb.sh

install: deb
	sudo apt install ./$(DEB)

uninstall:
	sudo apt remove system-update

clean:
	rm -rf build dist
	find . -name __pycache__ -prune -exec rm -rf {} +
