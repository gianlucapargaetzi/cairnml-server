"""The logging setup must not stop the server from starting where the log folder cannot be written."""

import logging
import logging.config
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from apiserver.config.basic import without_unwritable_file_handlers


def config_with_file(filename):
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "console": {"class": "logging.StreamHandler"},
            "text_file": {"class": "logging.handlers.RotatingFileHandler", "filename": filename},
        },
        "root": {"handlers": ["console", "text_file"], "level": "INFO"},
        "loggers": {"urllib3": {"handlers": ["console", "text_file"], "level": "WARN"}, "quiet": {"handlers": []}},
    }


class TestLoggingSetup(TestCase):
    def test_a_writable_folder_keeps_the_file_handler(self):
        with TemporaryDirectory() as folder:
            config = config_with_file(os.path.join(folder, "logs", "api.log"))
            self.assertIs(without_unwritable_file_handlers(config), config)
            self.assertTrue((Path(folder) / "logs").is_dir())  # the folder was created

    def test_a_folder_that_cannot_be_written_leaves_the_file_handler_out_everywhere(self):
        config = config_with_file("/proc/no-such-folder/api.log")
        result = without_unwritable_file_handlers(config)
        self.assertEqual(set(result["handlers"]), {"console"})
        self.assertEqual(result["root"]["handlers"], ["console"])
        self.assertEqual(result["loggers"]["urllib3"]["handlers"], ["console"])
        self.assertEqual(result["loggers"]["quiet"]["handlers"], [])
        self.assertEqual(len(config["handlers"]), 2)  # the original is not changed

    def test_the_reduced_configuration_is_accepted_by_logging(self):
        root = logging.getLogger()
        self.addCleanup(setattr, root, "handlers", list(root.handlers))
        self.addCleanup(root.setLevel, root.level)
        result = without_unwritable_file_handlers(config_with_file("/proc/no-such-folder/api.log"))
        logging.config.dictConfig(result)  # raises ValueError for a handler that was dropped but is still named

    def test_it_works_on_the_parsed_configuration_files_not_only_on_plain_dicts(self):
        from pyhocon import ConfigFactory

        parsed = ConfigFactory.parse_string(
            'handlers { console { class: "logging.StreamHandler" }, text_file { class: "logging.handlers.RotatingFileHandler", '
            'filename: "/proc/no-such-folder/api.log" } }\n'
            'root { handlers: [console, text_file], level: INFO }\n'
            'loggers { quiet { level: WARN } }'
        )
        result = without_unwritable_file_handlers(parsed)
        self.assertEqual(set(result["handlers"]), {"console"})
        self.assertEqual(result["root"]["handlers"], ["console"])
