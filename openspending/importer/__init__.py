import logging
import traceback
from datetime import datetime

from urllib import urlopen
from messytables import CSVRowSet, headers_processor, \
  offset_processor

from openspending.model import Run, LogRecord
from openspending.model import meta as db
from openspending.validation.model import Invalid
from openspending.validation.data import convert_types

log = logging.getLogger(__name__)


class BaseImporter(object):

    def __init__(self, source):
        self.source = source
        self.dataset = source.dataset
        self.errors = 0
        self.row_number = None

    def run(self,
            dry_run=False,
            max_lines=None,
            raise_errors=False,
            **kwargs):

        self.dry_run = dry_run
        self.raise_errors = raise_errors

        # Get unique key for this dataset
        self.key = self._get_unique_key()
        # If this is a dry run we need to check uniqueness
        # Initialize unique check dictionary
        if dry_run:
            self.unique_check = {}

        before_count = len(self.dataset)

        self.row_number = 0
        
        # If max_lines is set we're doing a sample, not an import
        operation = Run.OPERATION_SAMPLE if dry_run else Run.OPERATION_IMPORT
        self._run = Run(operation, Run.STATUS_RUNNING,
                        self.dataset, self.source)
        db.session.add(self._run)
        db.session.commit()
        log.info("Run reference: #%s", self._run.id)

        try:
            for row_number, line in enumerate(self.lines, start=1):
                if max_lines and row_number >= max_lines:
                    break

                self.row_number = row_number
                self.process_line(line)
        except Exception as ex:
            self.log_exception(ex)
            if self.raise_errors:
                self._run.status = Run.STATUS_FAILED
                self._run.time_end = datetime.utcnow()
                db.session.commit()
                raise

        if self.row_number == 0:
            self.log_exception(ValueError("Didn't read any lines of data"),
                    error='')

        num_loaded = len(self.dataset) - before_count
        if not dry_run and not self.errors and \
                num_loaded < (self.row_number - 1):
            self.log_exception(ValueError("The number of entries loaded is "
                "smaller than the number of source rows read."),
                error="%s rows were read, but only %s entries created. "
                    "Check the unique key criteria, entries seem to overlap." \
                    % (self.row_number, num_loaded))

        if self.errors:
            self._run.status = Run.STATUS_FAILED
        else:
            self._run.status = Run.STATUS_COMPLETE
            log.info("Finished import with no errors!")
        self._run.time_end = datetime.utcnow()
        self.dataset.updated_at = self._run.time_end
        db.session.commit()

    @property
    def lines(self):
        raise NotImplementedError("lines not implemented in BaseImporter")

    def _get_unique_key(self):
        """
        Return a list of unique keys for the dataset
        """
        return [k for k,v in self.dataset.mapping.iteritems()
                if v.get('key', False)]

    def process_line(self, line):
        if self.row_number % 1000 == 0:
            log.info('Imported %s lines' % self.row_number)

        try:
            data = convert_types(self.dataset.mapping, line)
            if not self.dry_run:
                self.dataset.load(data)
            else:
                # Check uniqueness
                unique_value = ', '.join([unicode(data[k]) for k in self.key])
                if self.unique_check.has_key(unique_value):
                    # Log the error (with the unique key represented as
                    # a dictionary)
                    self.log_exception(
                        ValueError("Unique key constraint not met"),
                        error="%s is not a unique key" % unique_value)
                self.unique_check[unique_value] = True
        except Invalid as invalid:
            for child in invalid.children:
                self.log_invalid_data(child)
            if self.raise_errors:
                raise
        except Exception as ex:
            self.log_exception(ex)
            if self.raise_errors:
                raise

    def log_invalid_data(self, invalid):
        log_record = LogRecord(self._run, LogRecord.CATEGORY_DATA,
                               logging.ERROR, invalid.msg)
        log_record.attribute = invalid.node.name
        log_record.column = invalid.column
        log_record.value = invalid.value
        log_record.data_type = invalid.datatype

        msg = "'%s' (%s) could not be generated from column '%s'" \
              " (value: %s): %s"
        msg = msg % (invalid.node.name, invalid.datatype, \
                     invalid.column, invalid.value, invalid.msg)
        log.warn(msg)
        self._log(log_record)

    def log_exception(self, exception, error=None):
        log_record = LogRecord(self._run, LogRecord.CATEGORY_SYSTEM,
                               logging.ERROR, str(exception))
        if error is not None:
            log_record.error = error
        else:
            log_record.error = traceback.format_exc()
        log.error(unicode(exception))
        self._log(log_record)

    def _log(self, log_record):
        self.errors += 1
        log_record.row = self.row_number
        db.session.add(log_record)
        db.session.commit()


class CSVImporter(BaseImporter):

    @property
    def lines(self):
        fh = urlopen(self.source.url)
        row_set = CSVRowSet('data', fh, window=3)
        headers = list(row_set.sample)[0]
        headers = [c.value for c in headers]
        row_set.register_processor(headers_processor(headers))
        row_set.register_processor(offset_processor(1))
        for row in row_set:
            yield dict([(c.column, c.value) for c in row])
