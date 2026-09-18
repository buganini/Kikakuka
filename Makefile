.PHONY: sync archive

sync:
	rsync -av8 --delete --exclude='.git/' --exclude='__pycache__/' FreekiCAD/ ../FreekiCAD/

archive:
	rm -f kikakuka-addon.zip
	cd kicad-addon && zip -X -r ../kikakuka-addon.zip metadata.json footprints resources
