from __future__ import annotations

# Re-export core AST components for backward compatibility
from core.ast_patcher_core import (
    ASTPatchError,
    DeletionTransformer,
    delete_definitions,
    inject_import_at_top,
)
from core.ast_extractor import (
    parse_llm_json_list,
    DefinitionCollector,
    collect_definitions,
    extract_function_source,
    extract_function_by_name,
    extract_imports_source,
    expand_entity_list,
)
from core.ast_taint_helpers import _get_expr_str
from core.ast_taint import (
    PythonASTTaintTracer,
    check_ast_taint,
    check_ast_taint_interprocedural,
)
