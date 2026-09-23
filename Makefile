.PHONY: sync archive archive-zips

METADATA_REPO_URL ?= git@gitlab.com:buganini/metadata.git
METADATA_WORKDIR ?= workdir/metadata

sync:
	rsync -av8 --delete --exclude='.git/' --exclude='__pycache__/' FreekiCAD/ ../FreekiCAD/

archive-zips:
	rm -f kikakuka-library.zip kikakuka-plugin.zip
	cd kicad-addon/library && zip -X -r ../../kikakuka-library.zip metadata.json footprints 3dmodels resources
	cd kicad-addon/plugin && zip -X -r ../../kikakuka-plugin.zip metadata.json plugins -x '*/__pycache__/*' '*.pyc'

archive: archive-zips
	mkdir -p $(dir $(METADATA_WORKDIR))
	@if [ -d "$(METADATA_WORKDIR)/.git" ]; then \
		git -C "$(METADATA_WORKDIR)" pull --ff-only; \
	else \
		git clone "$(METADATA_REPO_URL)" "$(METADATA_WORKDIR)"; \
	fi
	python3 build-kicad-library-metadata.py "$(METADATA_WORKDIR)"
	python3 build-kicad-plugin-metadata.py "$(METADATA_WORKDIR)"
