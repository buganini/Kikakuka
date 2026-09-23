.PHONY: sync archive

METADATA_REPO_URL ?= git@gitlab.com:buganini/metadata.git
METADATA_WORKDIR ?= workdir/metadata

sync:
	rsync -av8 --delete --exclude='.git/' --exclude='__pycache__/' FreekiCAD/ ../FreekiCAD/

archive:
	rm -f kikakuka-addon.zip
	cd kicad-library && zip -X -r ../kikakuka-addon.zip metadata.json footprints resources
	mkdir -p $(dir $(METADATA_WORKDIR))
	@if [ -d "$(METADATA_WORKDIR)/.git" ]; then \
		git -C "$(METADATA_WORKDIR)" pull --ff-only; \
	else \
		git clone "$(METADATA_REPO_URL)" "$(METADATA_WORKDIR)"; \
	fi
	python3 build-kicad-library-metadata.py "$(METADATA_WORKDIR)"
