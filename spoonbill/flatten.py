import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field, is_dataclass
from typing import List, Mapping, Sequence

from spoonbill.common import JOINABLE
from spoonbill.i18n import _
from spoonbill.spec import Table
from spoonbill.utils import generate_row_id, get_pointer, get_root

LOGGER = logging.getLogger("spoonbill")


@dataclass
class TableFlattenConfig:
    """Table specific flattening configuration

    :param split: Split child arrays to separate tables
    :param pretty_headers: Use human friendly headers extracted from schema
    :param headers: User edited headers to override automatically extracted
    :param unnest: List of columns to output from child to parent table
    :param repeat: List of columns to clone in child tables
    """

    split: bool
    pretty_headers: bool = False
    headers: Mapping[str, str] = field(default_factory=dict)
    only: List[str] = field(default_factory=list)
    repeat: List[str] = field(default_factory=list)
    unnest: List[str] = field(default_factory=list)
    name: str = ""


@dataclass
class FlattenOptions:

    """Whole flattening process configuration

    :param selection: List of selected tables to extract from data
    :param count: Include number of rows in child table in each parent table
    """

    selection: Mapping[str, TableFlattenConfig]
    count: bool = False

    def __post_init__(self):
        for name, table in self.selection.items():
            if not is_dataclass(table):
                self.selection[name] = TableFlattenConfig(**table)


class Flattener:
    """Configurable data flattener

    :param options: Flattening options
    :param tables: Analyzed tables data
    """

    def __init__(self, options: FlattenOptions, tables: Mapping[str, Table]):
        if not is_dataclass(options):
            options = FlattenOptions(**options)
        self.options = options
        self.tables = tables

        self._lookup_cache = {}
        self._types_cache = {}
        self._path_cache = {}
        self._init()

    def _init_table_cache(self, tables, table):
        if table.total_rows == 0:
            return

        name = table.name
        options = self.options.selection[name]
        split = options.split
        tables[name] = table

        for p in table.path:
            self._path_cache[p] = table
        for path in table.types:
            self._types_cache[path] = table

        cols = table if split else table.combined_columns
        for path in cols:
            self._lookup_cache[path] = table

    def _init_options(self, tables):
        """"""
        for table in tables.values():
            name = table.name
            count = self.options.count
            options = self.options.selection[name]
            unnest = options.unnest
            split = options.split
            repeat = options.repeat
            if count:
                for array in table.arrays:
                    parts = array.split("/")
                    key = parts[-1]
                    title = f"{key}Count"
                    parts[-1] = title
                    path = "/".join(parts)
                    target = self._types_cache.get(array) or table
                    target.add_column(
                        path,
                        {"title": f"{key} count"},
                        "integer",
                        parent={},
                    )
                    if table.arrays[array] > 0:
                        target.inc_column(path)

            if unnest:
                for col_id in unnest:
                    col = table.combined_columns[col_id]
                    table.columns[col_id] = col

            if repeat:
                for col_id in repeat:
                    columns = table.columns if split else table.combined_columns
                    title = table.titles.get(col_id)
                    col = columns.get(col_id)
                    if not col:
                        LOGGER.warning(
                            _("Ignoring repeat column {} because it is not in table {}").format(col_id, name)
                        )
                        continue
                    for c_name in table.child_tables:
                        child_table = self.tables.get(c_name)
                        child_table.columns[col_id] = col
                        child_table.titles[col_id] = title

    def _init(self):
        # init cache and filter only selected tables
        tables = {}
        for name, table in self.tables.items():
            if name not in self.options.selection:
                continue
            self._init_table_cache(tables, table)
            split = self.options.selection[name].split
            if split:
                for c_name in table.child_tables:
                    c_table = self.tables[c_name]
                    self.options.selection[c_name] = TableFlattenConfig(split=True)
                    self._init_table_cache(tables, c_table)
        self.tables = tables
        self._init_options(self.tables)

    def flatten(self, releases):
        """Flatten releases

        :param releases: releases as iterable object
        :return: Iterator over mapping between table name and list of rows for each release
        """

        for counter, release in enumerate(releases):
            rows = defaultdict(list)
            to_flatten = deque([("", "", "", {}, release, {})])
            separator = "/"
            ocid = release["ocid"]
            top_level_id = release["id"]

            while to_flatten:
                abs_path, path, parent_key, parent, record, repeat = to_flatten.pop()
                table = self._path_cache.get(path)
                if table:
                    # Strict match /tender /parties etc., so this is a new row
                    row_id = generate_row_id(ocid, record.get("id", ""), parent_key, top_level_id)
                    new_row = {
                        "rowID": row_id,
                        "id": top_level_id,
                        "parentID": parent.get("id"),
                        "ocid": ocid,
                    }
                    if repeat:
                        new_row.update(repeat)
                    rows[table.name].append(new_row)

                for key, item in record.items():
                    pointer = separator.join((path, key))
                    table = self._lookup_cache.get(pointer) or self._types_cache.get(pointer)
                    if not table:
                        continue
                    item_type = table.types.get(pointer)
                    abs_pointer = separator.join((abs_path, key))
                    options = self.options.selection[table.name]
                    split = options.split

                    if pointer in options.repeat:
                        repeat[pointer] = item

                    if isinstance(item, dict):
                        to_flatten.append((abs_pointer, pointer, key, record, item, repeat))
                    elif isinstance(item, list):
                        if item_type == JOINABLE:
                            value = JOINABLE.join(item)
                            rows[table.name][-1][pointer] = value
                        else:
                            if self.options.count:
                                abs_pointer = get_pointer(
                                    pointer,
                                    abs_path,
                                    key,
                                    split,
                                    separator,
                                    table.is_root,
                                )
                                abs_pointer += "Count"
                                if abs_pointer in table:
                                    rows[table.name][-1][abs_pointer] = len(item)
                            for index, value in enumerate(item):
                                if isinstance(value, dict):
                                    abs_pointer = separator.join((abs_path, key, str(index)))
                                    to_flatten.append(
                                        (
                                            abs_pointer,
                                            pointer,
                                            key,
                                            record,
                                            value,
                                            repeat,
                                        )
                                    )
                    else:
                        if not table.is_root:
                            root = get_root(table)
                            unnest = self.options.selection[root.name].unnest
                            if unnest and abs_pointer in unnest:
                                rows[root.name][-1][abs_pointer] = item
                                continue
                        pointer = get_pointer(pointer, abs_path, key, split, separator, table.is_root)
                        rows[table.name][-1][pointer] = item
            yield counter, rows
