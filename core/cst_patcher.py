import libcst as cst
from typing import List, Optional, Set, Union
import logging

logger = logging.getLogger(__name__)



class RemovalTransformer(cst.CSTTransformer):
    """
    CST Transformer to remove specific functions or classes from the code
    while preserving formatting and comments of surrounding code.
    """
    def __init__(self, names_to_remove: List[str]):
        # Pulizia nomi per gestire eventuali percorsi completi
        self.names = set(name.split('.')[-1] for name in names_to_remove)
        self.removed_count = 0 
        logger.debug(f"CST: Target rimozione: {self.names}")

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> Union[cst.FunctionDef, cst.RemovalSentinel]:
        if original_node.name.value in self.names:
            self.removed_count += 1 # <-- CRITICO: Incrementa qui
            return cst.RemoveFromParent()
        return updated_node

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> Union[cst.ClassDef, cst.RemovalSentinel]:
        if original_node.name.value in self.names:
            self.removed_count += 1 # <-- CRITICO: Incrementa qui
            return cst.RemoveFromParent()
        return updated_node    

    def leave_Assign(self, original_node: cst.Assign, updated_node: cst.Assign) -> Union[cst.Assign, cst.RemovalSentinel]:
        # Logica non distruttiva per assegnazioni multiple e spacchettamenti (Bug 24)
        new_targets = []
        removed_any = False
        
        for target in updated_node.targets:
            target_node = target.target
            if isinstance(target_node, cst.Name):
                if target_node.value in self.names:
                    removed_any = True
                    continue
                else:
                    new_targets.append(target)
            elif isinstance(target_node, (cst.Tuple, cst.List)):
                new_elements = []
                removed_elements_indices = []
                for idx, element in enumerate(target_node.elements):
                    if isinstance(element.value, cst.Name) and element.value.value in self.names:
                        removed_any = True
                        removed_elements_indices.append(idx)
                    else:
                        new_elements.append(element)
                        
                if removed_elements_indices:
                    if new_elements:
                        rhs = updated_node.value
                        if isinstance(rhs, (cst.Tuple, cst.List)) and len(rhs.elements) == len(target_node.elements):
                            new_rhs_elements = [el for i, el in enumerate(rhs.elements) if i not in removed_elements_indices]
                            new_rhs = rhs.with_changes(elements=new_rhs_elements)
                            new_target_node = target_node.with_changes(elements=new_elements)
                            new_targets.append(target.with_changes(target=new_target_node))
                            updated_node = updated_node.with_changes(value=new_rhs)
                        else:
                            new_targets.append(target)
                    else:
                        pass
            else:
                new_targets.append(target)
                
        if removed_any:
            self.removed_count += 1
            if not new_targets:
                return cst.RemoveFromParent()
            return updated_node.with_changes(targets=new_targets)
            
        return updated_node


class ImportInjectionTransformer(cst.CSTTransformer):
    """
    CST Transformer to inject import statements at the top of a file
    while avoiding duplicates and preserving formatting.
    """
    def __init__(self, import_statements: List[str]):
        self.import_statements = import_statements
        self.injected = False
        self.existing_imports: Set[str] = set()

    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool:
        """Track existing imports to avoid duplicates."""
        if isinstance(node.names, cst.ImportStar):
            return True
        if isinstance(node.names, (list, tuple)):
            for name in node.names:
                if isinstance(name, cst.ImportAlias):
                    self.existing_imports.add(name.name.value if isinstance(name.name, cst.Name) else str(name.name))
        return True

    def visit_Import(self, node: cst.Import) -> bool:
        """Track existing imports to avoid duplicates."""
        for name in node.names:
            if isinstance(name, cst.ImportAlias):
                self.existing_imports.add(name.name.value if isinstance(name.name, cst.Name) else str(name.name))
        return True

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        """Inject import statements at the beginning of the module."""
        if self.injected or not self.import_statements:
            return updated_node

        new_body = list(updated_node.body)
        new_imports = []

        for import_stmt in self.import_statements:
            import_stmt = import_stmt.strip()
            if not import_stmt or import_stmt in self.existing_imports:
                continue

            try:
                parsed_import = cst.parse_statement(import_stmt)
                new_imports.append(parsed_import)
            except Exception:
                continue

        if new_imports:
            new_body = new_imports + new_body
            self.injected = True

        return updated_node.with_changes(body=new_body)


def remove_definitions_cst(source_code: str, names: List[str]) -> str:
    """
    Removes function or class definitions using LibCST, preserving comments 
    and original formatting.
    
    Args:
        source_code: The original source code string
        names: List of function/class names to remove
        
    Returns:
        Modified source code string
        
    Raises:
        Exception: Propagates LibCST parsing or transformation errors
    """
    try:
        tree = cst.parse_module(source_code)
        transformer = RemovalTransformer(names)
        modified_tree = tree.visit(transformer)
        
        # LOGICA DI VALIDAZIONE RIGOROSA
        if transformer.removed_count == 0 and len(names) > 0:
            raise RuntimeError(
                f"CST Removal Error: Nessuna entità rimossa per i target {names}. "
                "Possibile mismatch tra i nomi richiesti e la struttura del file."
            )
            
        logger.debug(f"CST: Rimozioni totali effettuate: {transformer.removed_count}")
        return modified_tree.code
    except Exception as e:
        logger.warning(f"CST Error: {e}")
        raise e

def inject_imports(source_code: str, import_statements: List[str]) -> str:
    """
    Injects import statements at the top of a file using LibCST,
    while preserving formatting and avoiding duplicates.
    
    Args:
        source_code: The original source code string
        import_statements: List of import statement strings to inject
        
    Returns:
        Modified source code string with injected imports
        
    Raises:
        Exception: Propagates LibCST parsing or transformation errors
    """
    try:
        tree = cst.parse_module(source_code)
        transformer = ImportInjectionTransformer(import_statements)
        modified_tree = tree.visit(transformer)
        return modified_tree.code
    except Exception as e:
        logger.warning(f"CST Import Injection Error: {e}")
        raise e
    

class GlobalImportRefactorTransformer(cst.CSTTransformer):
    def __init__(self, entities_to_move: List[str], old_module: str, new_module: str):
        self.entities_to_move = set(entities_to_move)
        self.old_module = old_module
        self.new_module = new_module
        
        # Generiamo dinamicamente i possibili modi in cui il modulo viene importato
        # es: 'yt_dlp.utils._utils', '._utils', '..utils' (se supportato)
        self.source_variants = {old_module}
        if "." in old_module:
            self.source_variants.add(old_module.split('.')[-1]) # l'ultimo pezzo
            self.source_variants.add("." + old_module.split('.')[-1]) # relativo

    def leave_ImportFrom(self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom) -> Union[cst.ImportFrom, cst.FlattenSentinel]:
        # Calcolo del nome del modulo nell'import attuale
        module_name = ""
        if original_node.module:
            module_name = cst.helpers.get_full_name_for_node(original_node.module)
        
        dots = "." * len(original_node.relative)
        full_import_path = f"{dots}{module_name}"

        # Se l'import non corrisponde a quello che stiamo spostando, non toccare nulla
        if full_import_path not in self.source_variants and module_name not in self.source_variants:
            return updated_node

        to_move = []
        to_keep = []

        if isinstance(original_node.names, cst.ImportStar):
            return updated_node

        for import_alias in original_node.names:
            if import_alias.name.value in self.entities_to_move:
                to_move.append(import_alias)
            else:
                to_keep.append(import_alias)

        if not to_move:
            return updated_node

        # Creazione del nuovo import verso il nuovo modulo (B) con parentesi dinamiche (Bug 12)
        lpar = original_node.lpar if len(to_move) > 1 else None
        rpar = original_node.rpar if len(to_move) > 1 else None

        new_import_node = cst.ImportFrom(
            module=cst.parse_expression(self.new_module),
            names=to_move,
            lpar=lpar,
            rpar=rpar,
            leading_lines=[cst.EmptyLine(indent=original_node.leading_lines != [])]
        )

        if not to_keep:
            return new_import_node
        else:
            lpar_keep = original_node.lpar if len(to_keep) > 1 else None
            rpar_keep = original_node.rpar if len(to_keep) > 1 else None
            modified_original = updated_node.with_changes(
                names=to_keep,
                lpar=lpar_keep,
                rpar=rpar_keep
            )
            return cst.FlattenSentinel([modified_original, new_import_node])


# ---------------------------------------------------------------------------
# MethodPrependTransformer — insert statements at the start of a method body
# ---------------------------------------------------------------------------

class MethodPrependTransformer(cst.CSTTransformer):
    """
    Insert statements at the beginning of a function or method body.

    If class_name is provided, only targets methods of that class.
    If class_name is empty, targets module-level functions.
    Insertion happens after the leading docstring (if any), so the
    docstring is preserved as the first statement.
    """

    def __init__(self, method_name: str, code_to_prepend: str, class_name: str = ""):
        self.method_name   = method_name
        self.class_name    = class_name
        self.code_to_prepend = code_to_prepend.strip()
        self._class_stack: list[str] = []
        self.applied = False

    def visit_ClassDef(self, node: cst.ClassDef) -> Optional[bool]:
        self._class_stack.append(node.name.value)
        return True

    def leave_ClassDef(
        self, original_node: cst.ClassDef, updated_node: cst.ClassDef
    ) -> cst.ClassDef:
        if self._class_stack:
            self._class_stack.pop()
        return updated_node

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if original_node.name.value != self.method_name:
            return updated_node

        if self.class_name:
            if not self._class_stack or self._class_stack[-1] != self.class_name:
                return updated_node
        else:
            if self._class_stack:   # caller wants a module-level fn, not a method
                return updated_node

        old_body = updated_node.body
        if not isinstance(old_body, cst.IndentedBlock):
            return updated_node

        # Parse new code as module-level statements so LibCST handles indentation
        try:
            new_stmts = cst.parse_module(self.code_to_prepend + "\n").body
        except Exception as exc:
            logger.warning(f"MethodPrepend: could not parse code to prepend: {exc}")
            return updated_node

        # Skip leading docstring — insert after it so it stays first
        existing = list(old_body.body)
        insert_pos = 0
        if existing:
            first = existing[0]
            if isinstance(first, cst.SimpleStatementLine) and first.body:
                if isinstance(first.body[0], cst.Expr):
                    val = first.body[0].value
                    if isinstance(val, (cst.SimpleString, cst.ConcatenatedString,
                                       cst.FormattedString)):
                        insert_pos = 1

        combined = existing[:insert_pos] + list(new_stmts) + existing[insert_pos:]
        self.applied = True
        return updated_node.with_changes(
            body=old_body.with_changes(body=combined)
        )


def prepend_to_method(
    source_code: str,
    method_name: str,
    code_to_prepend: str,
    class_name: str = "",
) -> str:
    """
    Insert *code_to_prepend* at the start of *method_name*'s body.

    Args:
        source_code:      Original Python source.
        method_name:      Function or method name to target.
        code_to_prepend:  Python source lines to insert (module-level style).
        class_name:       If given, only matches methods of this class.
                          If empty, matches module-level functions.

    Returns:
        Modified source code string.

    Raises:
        RuntimeError: if the target method is not found.
        Exception:    propagates LibCST errors.
    """
    try:
        tree = cst.parse_module(source_code)
        transformer = MethodPrependTransformer(method_name, code_to_prepend, class_name)
        modified = tree.visit(transformer)
        if not transformer.applied:
            target = f"{class_name}.{method_name}" if class_name else method_name
            raise RuntimeError(
                f"prepend_to_method: '{target}' not found in source"
            )
        return modified.code
    except Exception as exc:
        logger.warning(f"CST prepend_to_method error: {exc}")
        raise


class InPlaceReplacementTransformer(cst.CSTTransformer):
    """
    CST Transformer to replace a specific function, class, or method definition
    in-place within its parent container (Module or IndentedBlock).
    """
    def __init__(self, symbol_target: str, replacement_code: str):
        self.symbol_target = symbol_target
        self.replacement_code = replacement_code.strip()
        if "." in symbol_target:
            self.class_name, self.method_name = symbol_target.rsplit(".", 1)
        else:
            self.class_name, self.method_name = "", symbol_target
        self._class_stack: List[str] = []
        self.applied = False

    def visit_ClassDef(self, node: cst.ClassDef) -> Optional[bool]:
        self._class_stack.append(node.name.value)
        return True

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        if self._class_stack:
            self._class_stack.pop()
        return updated_node

    def leave_IndentedBlock(self, original_node: cst.IndentedBlock, updated_node: cst.IndentedBlock) -> cst.IndentedBlock:
        # If we are inside the target class body, look for the target method to replace in-place
        if self.class_name and self._class_stack and self._class_stack[-1] == self.class_name:
            new_body = []
            for stmt in updated_node.body:
                if isinstance(stmt, cst.FunctionDef) and stmt.name.value == self.method_name:
                    try:
                        parsed_stmts = cst.parse_module(self.replacement_code + "\n").body
                        new_body.extend(parsed_stmts)
                        self.applied = True
                    except Exception as exc:
                        logger.warning(f"InPlaceReplacementTransformer (method) parse failed: {exc}")
                        new_body.append(stmt)
                else:
                    new_body.append(stmt)
            return updated_node.with_changes(body=new_body)
        return updated_node

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        # If we are at module level, look for target class or function to replace in-place
        if not self.class_name:
            new_body = []
            for stmt in updated_node.body:
                is_match = False
                if isinstance(stmt, cst.FunctionDef) and stmt.name.value == self.method_name:
                    is_match = True
                elif isinstance(stmt, cst.ClassDef) and stmt.name.value == self.method_name:
                    is_match = True
                elif isinstance(stmt, cst.SimpleStatementLine):
                    for part in stmt.body:
                        if isinstance(part, cst.Assign):
                            for target in part.targets:
                                if isinstance(target.target, cst.Name) and target.target.value == self.method_name:
                                    is_match = True
                                    break
                
                if is_match:
                    try:
                        parsed_stmts = cst.parse_module(self.replacement_code + "\n").body
                        new_body.extend(parsed_stmts)
                        self.applied = True
                    except Exception as exc:
                        logger.warning(f"InPlaceReplacementTransformer (module) parse failed: {exc}")
                        new_body.append(stmt)
                else:
                    new_body.append(stmt)
            return updated_node.with_changes(body=new_body)
        return updated_node


def replace_symbol_in_place(source_code: str, symbol_target: str, replacement_code: str) -> str:
    """
    Replaces a class, function, or method definition in-place using LibCST.
    
    Args:
        source_code:      The original source code string
        symbol_target:    The name of the symbol (e.g. "my_func" or "MyClass.my_method")
        replacement_code: The new implementation code
        
    Returns:
        Modified source code string
    """
    try:
        tree = cst.parse_module(source_code)
        transformer = InPlaceReplacementTransformer(symbol_target, replacement_code)
        modified_tree = tree.visit(transformer)
        if not transformer.applied:
            raise RuntimeError(f"Symbol '{symbol_target}' not found in source for in-place replacement")
        return modified_tree.code
    except Exception as e:
        logger.warning(f"CST in-place replacement error: {e}")
        raise e