.PHONY: sync archive

sync:
	rsync -av8 --delete --exclude='.git/' --exclude='__pycache__/' FreekiCAD/ ../FreekiCAD/

archive:
	rm -f kikakuka-addon.zip
	rm -rf kicad-addon-publish
	cd kicad-addon && zip -X -r ../kikakuka-addon.zip metadata.json footprints resources
	python3 build-kicad-addon-metadata.py
