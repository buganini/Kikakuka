import os
import csv
import datetime

def type_mapper(v):
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y/%m/%d %H:%M:%S")
    if isinstance(v, datetime.date):
        return v.strftime("%Y/%m/%d")
    if isinstance(v, datetime.time):
        return v.strftime("%H:%M:%S")
    return v

class CSVLoader():
    def __init__(self, filename, delimiter=None, encoding=None):
        if encoding is None:
            encoding = "utf-8"
        if delimiter is None:
            delimiter = ","
        self.filename = filename
        self.csv_file = open(filename, encoding=encoding)
        self.csv_reader = csv.reader(self.csv_file, delimiter=delimiter)

    def rows(self):
        return self.csv_reader
    
    def close(self):
        self.csv_file.close()
    
class XLSXLoader():
    def __init__(self, filename, sheet):
        if sheet is None:
            sheet = 0
        from openpyxl import load_workbook
        self.filename = filename
        self.wb = load_workbook(filename=filename, data_only=True, read_only=True)
        self.sheet = self.wb.worksheets[sheet]

    def rows(self):
        for row in self.sheet.rows:
            yield [type_mapper(cell.value) for cell in row]
    
    def close(self):
        self.wb.close()

class XLSLoader():
    def __init__(self, filename, sheet):
        import xlrd
        if sheet is None:
            sheet = 0
        self.filename = filename
        self.book = xlrd.open_workbook(filename)
        self.sheet = self.book.sheet_by_index(sheet)

    def rows(self):
        for row_idx in range(self.sheet.nrows):
            yield [type_mapper(cell.value) for cell in self.sheet.row(row_idx)]

    def close(self):
        self.book.release_resources()
        self.book = None

def toMaps(loader, break_on_empty_row=False):
    max_column = -1
    max_row = -1
    table = {}

    for r, row in enumerate(loader.rows()):
        max_row = max(max_row, r)
        empty = True
        for c, v in enumerate(row):
            table[(r,c)] = v
            if v:
                empty = False
            max_column = max(max_column, c)
        if empty and break_on_empty_row:
            break

    loader.close()

    max_column += 1
    max_row += 1

    return table, max_column, max_row

def TableLoader(filename, force=None, delimiter=None, encoding=None, sheet=None):
    if force:
        if force == "csv":
            return CSVLoader(filename, delimiter=delimiter, encoding=encoding)
        # elif force == "xlsx":
        #     return XLSXLoader(filename, sheet=sheet)
        # elif force == "xls":
        #     return XLSLoader(filename, sheet=sheet)
    fn, ext = os.path.splitext(filename)
    if ext.lower() in (".csv", ".txt", ".log"):
        return CSVLoader(filename, delimiter=delimiter, encoding=encoding)
    # elif ext.lower() == ".xlsx":
    #     return XLSXLoader(filename, sheet=sheet)
    # elif ext.lower() == ".xls":
    #     return XLSLoader(filename, sheet=sheet)
    return None


if __name__=="__main__":
    import sys
    loader = TableLoader(sys.argv[1])
    for row in loader:
        print([c for c in row])
