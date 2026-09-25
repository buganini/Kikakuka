.PHONY: sync archive archive-source archive-zips

METADATA_REPO_URL ?= git@gitlab.com:buganini/metadata.git
METADATA_WORKDIR ?= workdir/metadata
KIKAKUKA_VERSION := $(shell sed -n 's/^VERSION = "\([^"]*\)"/\1/p' common.py)
KICAD_LIBRARY_VERSION := $(shell python3 -c 'import json; print(json.load(open("kicad-addon/library/metadata.json"))["versions"][0]["version"])')
KICAD_PLUGIN_VERSION := $(shell python3 -c 'import json; print(json.load(open("kicad-addon/plugin/metadata.json"))["versions"][0]["version"])')
SOURCE_ARCHIVE := Kikakuka-$(KIKAKUKA_VERSION).tar.gz
LIBRARY_ARCHIVE := kikakuka-library-$(KICAD_LIBRARY_VERSION).zip
PLUGIN_ARCHIVE := kikakuka-plugin-$(KICAD_PLUGIN_VERSION).zip

sync:
	rsync -av8L --delete --exclude='.git/' --exclude='__pycache__/' FreekiCAD/ ../FreekiCAD/

archive-source:
	@test -n "$(KIKAKUKA_VERSION)"
	@tmpdir=$$(mktemp -d); \
	trap 'rm -rf "$$tmpdir"' EXIT; \
	root="Kikakuka-$(KIKAKUKA_VERSION)"; \
	list="$$tmpdir/tracked-files"; \
	git ls-files --recurse-submodules | while IFS= read -r path; do \
		if [ -e "$$path" ] || [ -L "$$path" ]; then \
			printf '%s\n' "$$path"; \
		fi; \
	done > "$$list"; \
	mkdir -p "$$tmpdir/$$root"; \
	rsync -aLr --files-from="$$list" ./ "$$tmpdir/$$root/"; \
	tar --dereference -C "$$tmpdir" -czf "$(CURDIR)/$(SOURCE_ARCHIVE)" "$$root"

archive-zips:
	rm -f kikakuka-library.zip kikakuka-plugin.zip "$(LIBRARY_ARCHIVE)" "$(PLUGIN_ARCHIVE)"
	cd kicad-addon/library && zip -X -r "../../$(LIBRARY_ARCHIVE)" metadata.json footprints 3dmodels resources
	cd kicad-addon/plugin && zip -X -r "../../$(PLUGIN_ARCHIVE)" metadata.json plugins resources -x '*/__pycache__/*' '*.pyc'

archive: archive-source archive-zips
	mkdir -p $(dir $(METADATA_WORKDIR))
	@if [ -d "$(METADATA_WORKDIR)/.git" ]; then \
		git -C "$(METADATA_WORKDIR)" pull --ff-only; \
	else \
		git clone "$(METADATA_REPO_URL)" "$(METADATA_WORKDIR)"; \
	fi
	python3 build-kicad-library-metadata.py "$(METADATA_WORKDIR)"
	python3 build-kicad-plugin-metadata.py "$(METADATA_WORKDIR)"
	@if command -v sha256sum >/dev/null 2>&1; then \
		sha256sum "$(SOURCE_ARCHIVE)" "$(LIBRARY_ARCHIVE)" "$(PLUGIN_ARCHIVE)"; \
	else \
		shasum -a 256 "$(SOURCE_ARCHIVE)" "$(LIBRARY_ARCHIVE)" "$(PLUGIN_ARCHIVE)"; \
	fi
