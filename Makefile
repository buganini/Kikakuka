sync:
	rsync -av8 --delete --exclude='.git/' --exclude='__pycache__/' FreekiCAD/ ../FreekiCAD/
