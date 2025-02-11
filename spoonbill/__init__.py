import logging
from operator import attrgetter
from pathlib import Path

from ocdsextensionregistry import ProfileBuilder
from ocdskit.util import detect_format

from spoonbill.common import COMBINED_TABLES, CURRENT_SCHEMA_TAG, ROOT_TABLES, TABLE_THRESHOLD
from spoonbill.flatten import Flattener
from spoonbill.i18n import LOCALE, _
from spoonbill.stats import DataPreprocessor
from spoonbill.utils import get_reader, iter_file, resolve_file_uri
from spoonbill.writers import CSVWriter, XlsxWriter

LOGGER = logging.getLogger("spoonbill")


class FileAnalyzer:
    """Main utility for analyzing files

    :param workdir: Working directory
    :param schema: Json schema file to use with data
    :param root_tables: Path configuration which should become root tables
    :param combined_tables: Path configuration for tables with multiple sources
    :param pkg_type: Field name to access records
    :param language: Language to use for the human-readable headings
    :param table_threshold: The maximum number of elements in an array before it is split into a table
    """

    def __init__(
        self,
        workdir,
        schema=None,
        state_file=None,
        root_tables=ROOT_TABLES,
        combined_tables=COMBINED_TABLES,
        pkg_type="releases",
        language=LOCALE,
        table_threshold=TABLE_THRESHOLD,
    ):
        self.workdir = Path(workdir)
        self.multiple_values = False
        self.schema = schema
        self.root_tables = root_tables
        self.combined_tables = combined_tables
        self.language = language
        self.table_threshold = table_threshold
        if state_file:
            self.spec = DataPreprocessor.restore(state_file)
        else:
            self.spec = None
        self.pkg_type = pkg_type

    def analyze_file(self, filenames, with_preview=True):

        """Analyze provided file
        :param filename: Input filename
        :param with_preview: Generate preview during analysis
        """
        if not isinstance(filenames, list):
            filenames = [filenames]

        path = self.workdir / filenames[0]
        (
            input_format,
            _is_concatenated,
            _is_array,
        ) = detect_format(path=filenames[0], reader=get_reader(path))
        LOGGER.info(_("Input file is {}").format(input_format))
        self.multiple_values = _is_concatenated
        self.parse_schema(input_format, self.schema)

        for filename in filenames:
            path = self.workdir / filename
            if self.spec is None:
                self.spec = DataPreprocessor(
                    self.schema,
                    self.root_tables,
                    combined_tables=self.combined_tables,
                    language=self.language,
                    table_threshold=self.table_threshold,
                    multiple_values=self.multiple_values,
                )
            reader = get_reader(path)
            with reader(path, "rb") as fd:
                items = iter_file(fd, self.pkg_type, multiple_values=self.multiple_values)
                for count in self.spec.process_items(items, with_preview=with_preview):
                    yield fd.tell(), count

    def dump_to_file(self, filenames):
        """Save analyzed information to file

        :param filename: Output filename in working directory
        """
        if not isinstance(filenames, list):
            filenames = [filenames]
        filename = filenames[0] / str(len(filenames)) if len(filenames) > 1 else ""
        path = self.workdir / filename
        self.spec.dump(path)

    def parse_schema(self, input_format, schema=None):
        if schema:
            schema = resolve_file_uri(schema)
        if "release" in input_format:
            pkg_type = "releases"
            getter = attrgetter("release_package_schema")
        else:
            pkg_type = "records"
            getter = attrgetter("record_package_schema")
        if not schema:
            LOGGER.info(_("No schema provided, using version {}").format(CURRENT_SCHEMA_TAG))
            profile = ProfileBuilder(CURRENT_SCHEMA_TAG, {})
            schema = getter(profile)()
        title = schema.get("title", "").lower()
        if not title:
            raise ValueError(_("Incomplete schema, please make sure your data is correct"))
        if "package" in title:
            # TODO: is is a good way to get release/record schema
            schema = schema["properties"][pkg_type]["items"]

        self.schema = schema
        self.pkg_type = pkg_type


class FileFlattener:
    """Main utility for flattening files

    :param workdir: Working directory
    :param options: Flattening configuration
    :param analyzer: Analyzed data object
    :param pkg_type: Field name to access records
    :param csv: If True generate cvs files
    :param xlsx: Generate combined xlsx table
    :param language: Language to use for the human-readable headings
    """

    def __init__(
        self,
        workdir,
        options,
        analyzer=None,
        tables=None,
        pkg_type="releases",
        csv=None,
        xlsx="result.xlsx",
        language=LOCALE,
        multiple_values=False,
    ):
        self.tables = tables if tables else analyzer.spec.tables
        self.flattener = Flattener(options, self.tables, language=language)
        self.workdir = Path(workdir)
        # TODO: detect package, where?
        self.writers = []
        self.csv = csv
        self.xlsx = xlsx
        self.multiple_values = multiple_values if multiple_values else analyzer.multiple_values if analyzer else False
        self.pkg_type = pkg_type if pkg_type else analyzer.pkg_type if analyzer else "releases"

    def _flatten(self, filenames, writers):
        if not isinstance(filenames, list):
            filenames = [filenames]
        for filename in filenames:
            path = self.workdir / filename
            reader = get_reader(path)
            with reader(path, "rb") as fd:
                items = iter_file(fd, self.pkg_type, multiple_values=self.multiple_values)
                for count, data in self.flattener.flatten(items):
                    for table, rows in data.items():
                        for row in rows:
                            for wr in writers:
                                wr.writerow(table, row)
                    yield count

    def flatten_file(self, filename):
        """Flatten file

        :param filename: Input filename in working directory
        """
        workdir = self.workdir
        if isinstance(self.csv, Path):
            workdir = self.csv
        if not self.xlsx and self.csv:
            with CSVWriter(workdir, self.flattener.tables, self.flattener.options) as writer:
                for count in self._flatten(filename, [writer]):
                    yield count
        if self.xlsx and not self.csv:
            with XlsxWriter(self.workdir, self.flattener.tables, self.flattener.options, filename=self.xlsx) as writer:
                for count in self._flatten(filename, [writer]):
                    yield count

        if self.xlsx and self.csv:
            with XlsxWriter(
                self.workdir, self.flattener.tables, self.flattener.options, filename=self.xlsx
            ) as xlsx, CSVWriter(workdir, self.flattener.tables, self.flattener.options) as csv:
                for count in self._flatten(filename, [xlsx, csv]):
                    yield count


__all__ = ["FileFlattener", "FileAnalyzer"]
