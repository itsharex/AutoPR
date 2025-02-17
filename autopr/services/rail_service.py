import pkg_resources
import lxml.etree as ET

import json
import traceback
from typing import Callable, Any, Optional, TypeVar, Type

import pydantic

import guardrails as gr
from autopr.models.rail_objects import RailObject
from autopr.models.prompt_rails import PromptRail

import structlog

from autopr.repos.completions_repo import CompletionsRepo
from autopr.services.publish_service import PublishService
from guardrails.utils.constants import constants

log = structlog.get_logger()

RailObjectSubclass = TypeVar('RailObjectSubclass', bound=RailObject)
BaseModelSubclass = TypeVar('BaseModelSubclass', bound=pydantic.BaseModel)


class RailService:
    """
    Service for invoking guardrails according to PromptRail and RailObject subclasses.
    See PromptRail, RailObject, and [Guardrails docs](https://docs.guardrails.io/) for more information.

    To make a guardrails call:
    - define a RailObject subclass
    - define a PromptRail subclass
    - instantiate the PromptRail
    - call `rail_service.run_prompt_rail(rail)` with the instantiated PromptRail

    For example:

        class Colors(RailObject):
            output_spec = '<list name="colors"><string/></list>'

            colors: list[str]

        class MyPromptRail(PromptRail):
            output_type = Colors
            prompt_template = "What colors is {something}?"

            something: str

        rail = MyPromptRail(something="a zebra")
        colors = rail_service.run_prompt_rail(rail)

        print(colors)  # colors=['black', 'white']

    This service is responsible for:
    - Compiling prompts according to `PromptRail.prompt_template`, `RailObject.output_spec`,
      and `RailObject.get_rail_spec()`
    - Invoking a guardrail LLM calls,
      optionally after an ordinary LLM call if `PromptRail.two_step` is True
    - Parsing the guardrail LLM response into a RailObject (pydantic) instance
    - Publishing the RailObject instance to the publish service
    - Keeping `publish_service` informed of what's going on


    Parameters
    ----------
    min_tokens: int
        Minimum number of tokens to leave in the context window to allow for response
    context_limit: int
        Context window token size limit
    num_reasks: int
        Number of times to re-ask the guardrail if it fails
    temperature: float
        Temperature to use for guardrails calls
    raw_system_prompt: str
        System prompt to use for ordinary LLM calls (if `PromptRail.two_step` is True)
    """

    _constants_imported = False

    def __init__(
        self,
        completions_repo: CompletionsRepo,
        publish_service: PublishService,
        min_tokens: int = 1000,
        context_limit: int = 8192,
        num_reasks: int = 2,
        temperature: float = 0.8,
        raw_system_prompt: str = 'You are a software developer and git nerd, a helpful planning and coding assistant.',
    ):
        self.completions_repo = completions_repo
        self.publish_service = publish_service
        self.min_tokens = min_tokens
        self.context_limit = context_limit
        self.num_reasks = num_reasks
        self.temperature = temperature
        self.raw_system_prompt = raw_system_prompt

        self._import_constants()

    def _import_constants(self):
        if self._constants_imported:
            return

        constants_file = pkg_resources.resource_filename('autopr', 'constants.xml')

        with open(constants_file, "r") as f:
            xml = f.read()

        parser = ET.XMLParser(encoding="utf-8")
        parsed_constants = ET.fromstring(xml, parser=parser)

        for child in parsed_constants:
            if isinstance(child, ET._Comment):
                continue
            if isinstance(child, str):
                continue

            constant_name = child.tag
            constant_value = child.text
            constants[constant_name] = constant_value

        self._constants_imported = True

    def run_rail_string(
        self,
        rail_spec: str,
        prompt_params: dict[str, Any],
        heading: str = "",
    ) -> Optional[dict[str, Any]]:
        """
        Run a guardrails call with the given rail spec and prompt parameters.
        """
        title_heading = heading[0].upper() + heading[1:]
        self.publish_service.start_section(f"🛤 Running {heading} rail")

        instructions = self.get_rail_instructions(rail_spec, prompt_params)
        if instructions.strip():
            self.publish_service.publish_code_block(
                heading='Instructions',
                code=instructions,
                language='xml',  # xml for nice guardrails highlighting
            )

        str_prompt = self.get_rail_message(rail_spec, prompt_params)
        self.publish_service.publish_code_block(
            heading='Prompt',
            code=str_prompt,
            language='xml',  # xml for nice guardrails highlighting
        )

        def completion_func(prompt: str, instructions: str):
            return self.completions_repo.complete(
                prompt=prompt,
                system_prompt=instructions,
                temperature=self.temperature,
            )

        try:
            pr_guard = gr.Guard.from_rail_string(
                rail_spec,  # make sure to import custom validators before this
                num_reasks=self.num_reasks,
            )

            log.debug(
                'Running rail',
                rail_spec=rail_spec,
                prompt_params=prompt_params,
            )
            # Invoke guardrails
            raw_o, dict_o = pr_guard(
                completion_func,
                prompt_params=prompt_params
            )
        except Exception:
            log.exception('Error running rail',
                          prompt=str_prompt)
            self.publish_service.publish_code_block(
                heading='Error',
                code=traceback.format_exc(),
                language='python',
            )
            self.publish_service.end_section(f"💥 {title_heading} derailed (guardrails error)")
            return None

        log.debug('Ran rail',
                  raw_output=raw_o,
                  dict_output=dict_o)
        self.publish_service.publish_code_block(
            heading='Raw output',
            code=raw_o,
            language='json',
        )

        if dict_o is None:
            log.warning(f'Got None from rail',
                        rail_spec=rail_spec,
                        prompt_params=prompt_params)
            self.publish_service.end_section(f"💥 {title_heading} derailed (guardrails returned None)")
            return None

        self.publish_service.publish_code_block(
            heading='Parsed output',
            code=json.dumps(dict_o, indent=2),
            language='json',
        )
        self.publish_service.end_section(f"🛤 Ran {heading} rail")

        return dict_o

    def run_rail_model(
        self,
        model: Type[BaseModelSubclass],
        rail_spec: str,
        prompt_params: dict[str, Any]
    ) -> Optional[BaseModelSubclass]:
        """
        Run a guardrails call with a pydantic model to parse the response into.
        """
        self.publish_service.start_section(f"🛤 Running {model.__name__} on rail")

        def completion_func(prompt: str, instructions: str):
            return self.completions_repo.complete(
                prompt=prompt,
                system_prompt=instructions,
                temperature=self.temperature,
            )

        pr_guard = gr.Guard.from_rail_string(
            rail_spec,  # make sure to import custom validators before this
            num_reasks=self.num_reasks,
        )

        prompt = self.get_rail_message(rail_spec, prompt_params)
        log.debug('Running rail',
                  rail_model=model.__name__,
                  rail_message=prompt)
        self.publish_service.publish_code_block(
            heading='Prompt',
            code=prompt,
            language='xml',  # xml for nice guardrails highlighting
        )

        # Invoke guardrails
        try:
            raw_o, dict_o = pr_guard(
                completion_func,
                prompt_params=prompt_params,
            )
        except Exception:
            self.publish_service.publish_code_block(
                heading='Error',
                code=traceback.format_exc(),
                language='python',
                default_open=True,
            )
            self.publish_service.end_section(f"💥 {model.__name__} derailed (guardrails error)")
            log.exception(f'Guardrails threw an exception',
                          rail_model=model.__name__,
                          rail_message=prompt)
            return None

        log.debug('Ran rail',
                  rail_model=model.__name__,
                  raw_output=raw_o,
                  dict_output=dict_o)

        self.publish_service.publish_code_block(
            heading='Raw output',
            code=raw_o,
            language='json',
        )

        if dict_o is None:
            self.publish_service.end_section(f"💥 {model.__name__} derailed (guardrails returned None)")
            log.warning(f'Got None from rail',
                        rail_model=model.__name__,
                        raw_output=raw_o)
            return None

        self.publish_service.publish_code_block(
            heading='Parsed output',
            code=json.dumps(dict_o, indent=2),
            language='json',
        )

        # Parse the output into a pydantic object
        try:
            parsed_obj = model.parse_obj(dict_o)
            self.publish_service.publish_code_block(
                heading='Validated output',
                code=parsed_obj.json(indent=2),
                language='json',
            )
            self.publish_service.end_section(f"🛤 Ran {model.__name__} on rail")
            return parsed_obj
        except pydantic.ValidationError:
            log.warning(f'Got invalid output from rail',
                        rail_object=model.__name__,
                        raw_output=raw_o,
                        dict_output=dict_o)
            self.publish_service.publish_code_block(
                heading='Error',
                code=traceback.format_exc(),
                language='python',
                default_open=True,
            )
            self.publish_service.end_section(f"💥 {model.__name__} derailed (validation error)")
            return None

    def run_rail_object(
        self,
        rail_object: Type[RailObjectSubclass],
        raw_document: str
    ) -> Optional[RailObjectSubclass]:
        """
        Transforms the `raw_document` into a pydantic instance described by `rail_object`.
        """
        rail_spec = rail_object.get_rail_spec()
        return self.run_rail_model(
            model=rail_object,
            rail_spec=rail_spec,
            prompt_params={
                'raw_document': raw_document,
            },
        )

    def run_prompt_rail(
        self,
        rail: PromptRail
    ) -> Optional[RailObject]:
        """
        Runs a PromptRail, asking the LLM a question and parsing the response into `PromptRail.output_type`.

        :param rail:
        :return:
        """
        # Make sure the prompt is not too long
        max_length = self.context_limit - self.min_tokens
        success = rail.ensure_token_length(max_length)
        if not success:
            return None

        # Run the rail
        prompt = rail.get_prompt_message()
        if rail.two_step:
            initial_prompt = prompt
            self.publish_service.start_section(f"💬 Asking for {rail.__class__.__name__}")

            self.publish_service.publish_code_block(
                heading="Prompt",
                code=prompt,
                language="",
            )
            prompt = self.completions_repo.complete(
                prompt=initial_prompt,
                system_prompt=self.raw_system_prompt,
            )
            self.publish_service.publish_code_block(
                heading="Response",
                code=prompt,
                language="",
            )
            self.publish_service.end_section(f"💬 Asked for {rail.__class__.__name__}")
        return self.run_rail_object(rail.output_type, prompt)

    @staticmethod
    def get_rail_instructions(
        rail_spec: str,
        prompt_params: dict[str, Any]
    ) -> str:
        pr_guard = gr.Guard.from_rail_string(rail_spec)
        return str(pr_guard.instructions.format(**prompt_params))

    @staticmethod
    def get_rail_message(
        rail_spec: str,
        prompt_params: dict[str, Any]
    ) -> str:
        pr_guard = gr.Guard.from_rail_string(rail_spec)
        return str(pr_guard.prompt.format(**prompt_params))
