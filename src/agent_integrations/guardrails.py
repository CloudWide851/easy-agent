from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from agent_common.models import GuardrailDecision, RunContext

ToolGuardrail = Callable[[str, dict[str, Any], RunContext], GuardrailDecision]
OutputGuardrail = Callable[[Any, RunContext], GuardrailDecision]


class GuardrailViolation(RuntimeError):
    def __init__(self, stage: str, decision: GuardrailDecision) -> None:
        super().__init__(f"{stage} guardrail '{decision.guardrail}' blocked execution: {decision.reason}")
        self.stage = stage
        self.decision = decision


class GuardrailEngine:
    def __init__(
        self,
        tool_input_hooks: list[str] | None = None,
        final_output_hooks: list[str] | None = None,
    ) -> None:
        self._tool_input_registry: dict[str, ToolGuardrail] = {
            'block_shell_metacharacters': self._block_shell_metacharacters,
        }
        self._final_output_registry: dict[str, OutputGuardrail] = {
            'require_non_empty_output': self._require_non_empty_output,
            'block_secret_leaks': self._block_secret_leaks,
        }
        self.tool_input_hooks = tool_input_hooks or ['block_shell_metacharacters']
        self.final_output_hooks = final_output_hooks or ['require_non_empty_output', 'block_secret_leaks']

    def check_tool_input(self, tool_name: str, arguments: dict[str, Any], context: RunContext) -> list[GuardrailDecision]:
        decisions: list[GuardrailDecision] = []
        for name in self.tool_input_hooks:
            handler = self._tool_input_registry[name]
            decisions.append(handler(tool_name, arguments, context))
        return decisions

    def check_final_output(self, output: Any, context: RunContext) -> list[GuardrailDecision]:
        decisions: list[GuardrailDecision] = []
        for name in self.final_output_hooks:
            handler = self._final_output_registry[name]
            decisions.append(handler(output, context))
        return decisions

    @staticmethod
    def ensure_allowed(stage: str, decisions: list[GuardrailDecision]) -> None:
        blocked = next((item for item in decisions if item.outcome == 'block'), None)
        if blocked is not None:
            raise GuardrailViolation(stage, blocked)

    def _block_shell_metacharacters(self, tool_name: str, arguments: dict[str, Any], context: RunContext) -> GuardrailDecision:
        del context
        lowered_text = ' '.join(_iter_strings(arguments)).lower()
        patterns = ['&&', '||', ';', '`', 'rm -rf', 'shutdown', 'format c:', 'del /f', 'powershell -enc']
        for pattern in patterns:
            if pattern in lowered_text:
                return GuardrailDecision(
                    outcome='block',
                    guardrail='block_shell_metacharacters',
                    reason=f"blocked suspicious token '{pattern}' before tool '{tool_name}'",
                    payload={'tool_name': tool_name, 'pattern': pattern},
                )
        return GuardrailDecision(
            outcome='allow',
            guardrail='block_shell_metacharacters',
            reason=f"tool '{tool_name}' input passed shell-token scan",
            payload={'tool_name': tool_name},
        )

    @staticmethod
    def _require_non_empty_output(output: Any, context: RunContext) -> GuardrailDecision:
        del context
        text = _stringify_output(output).strip()
        if not text:
            return GuardrailDecision(
                outcome='block',
                guardrail='require_non_empty_output',
                reason='final output was empty',
            )
        return GuardrailDecision(
            outcome='allow',
            guardrail='require_non_empty_output',
            reason='final output was non-empty',
            payload={'length': len(text)},
        )

    @staticmethod
    def _block_secret_leaks(output: Any, context: RunContext) -> GuardrailDecision:
        del context
        text = _stringify_output(output)
        secret_patterns = [
            r'sk-[A-Za-z0-9]{16,}',
            r'DEEPSEEK_API_KEY\s*=\s*\S+',
            r'AKIA[0-9A-Z]{16}',
        ]
        for pattern in secret_patterns:
            if re.search(pattern, text):
                return GuardrailDecision(
                    outcome='block',
                    guardrail='block_secret_leaks',
                    reason='final output matched a secret-like pattern',
                    payload={'pattern': pattern},
                )
        return GuardrailDecision(
            outcome='allow',
            guardrail='block_secret_leaks',
            reason='final output passed secret scan',
        )


def _iter_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        dict_values: list[str] = []
        for item in value.values():
            dict_values.extend(_iter_strings(item))
        return dict_values
    if isinstance(value, (list, tuple, set)):
        sequence_values: list[str] = []
        for item in value:
            sequence_values.extend(_iter_strings(item))
        return sequence_values
    return []


def _stringify_output(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        return ' '.join(_iter_strings(output))
    if isinstance(output, (list, tuple)):
        return ' '.join(_iter_strings(list(output)))
    return str(output)


