"""
ingestion — Universal document ingestion layer for the Revenue Analytics System.

Public API (everything the rest of the app needs):

    from modules.ingestion import parse, IngestionResult

    result = parse(file_obj, file_name)
    if result.success:
        df = result.df          # canonical schema DataFrame
        ...

The result integrates directly with the existing data_processor pipeline:
    data_processor._process_with_ingestion_layer(result)
"""

from .parser_factory import parse, IngestionResult

__all__ = ["parse", "IngestionResult"]
