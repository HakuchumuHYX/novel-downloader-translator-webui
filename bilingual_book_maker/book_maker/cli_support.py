from __future__ import annotations

import json
import os
from os import environ as env

OPENAI_DEFAULT_MODEL_LIST = ["gpt-5.2"]


def parse_prompt_arg(prompt_arg):
    prompt = None
    if prompt_arg is None:
        return prompt

    if prompt_arg.endswith(".md") and os.path.exists(prompt_arg):
        try:
            from promptdown import StructuredPrompt

            structured_prompt = StructuredPrompt.from_promptdown_file(prompt_arg)
            prompt = {}

            if hasattr(structured_prompt, "developer_message") and structured_prompt.developer_message:
                prompt["system"] = structured_prompt.developer_message
            elif hasattr(structured_prompt, "system_message") and structured_prompt.system_message:
                prompt["system"] = structured_prompt.system_message

            if hasattr(structured_prompt, "conversation") and structured_prompt.conversation:
                for message in structured_prompt.conversation:
                    if message.role.lower() == "user":
                        prompt["user"] = message.content
                        break

            if "user" not in prompt or not prompt["user"]:
                raise ValueError("PromptDown file must contain at least one user message")

            print(f"Successfully loaded PromptDown file: {prompt_arg}")

            if any(c not in prompt["user"] for c in ["{text}"]):
                raise ValueError("User message in PromptDown must contain `{text}` placeholder")

            return prompt
        except Exception as exc:
            raise ValueError(f"Failed to parse PromptDown file {prompt_arg}: {exc}") from exc

    if not any(prompt_arg.endswith(ext) for ext in [".json", ".txt", ".md"]):
        try:
            prompt = json.loads(prompt_arg)
        except json.JSONDecodeError:
            prompt = {"user": prompt_arg}
    elif os.path.exists(prompt_arg):
        if prompt_arg.endswith(".txt"):
            with open(prompt_arg, encoding="utf-8") as handle:
                prompt = {"user": handle.read()}
        elif prompt_arg.endswith(".json"):
            with open(prompt_arg, encoding="utf-8") as handle:
                prompt = json.load(handle)
    else:
        raise FileNotFoundError(f"{prompt_arg} not found")

    if prompt is None:
        raise ValueError("prompt is empty")
    if "user" not in prompt:
        raise ValueError("prompt must contain the key of `user`")
    if any(c not in prompt["user"] for c in ["{text}"]):
        raise ValueError("prompt must contain `{text}`")
    if (prompt.keys() - {"user", "system"}) != set():
        raise ValueError("prompt can only contain the keys of `user` and `system`")

    print("prompt config:", prompt)
    return prompt


def resolve_api_key(options):
    if options.model == "openai":
        openai_api_key = options.openai_key or env.get("BBM_OPENAI_API_KEY")
        if openai_api_key:
            return openai_api_key
        if options.ollama_model:
            return "ollama"
        raise Exception("OpenAI API key not provided, please google how to obtain it")
    if options.model == "caiyun":
        api_key = options.caiyun_key or env.get("BBM_CAIYUN_API_KEY")
        if not api_key:
            raise Exception("Please provide caiyun key")
        return api_key
    if options.model == "deepl":
        api_key = options.deepl_key or env.get("BBM_DEEPL_API_KEY")
        if not api_key:
            raise Exception("Please provide deepl key")
        return api_key
    if options.model.startswith("claude"):
        api_key = options.claude_key or env.get("BBM_CLAUDE_API_KEY")
        if not api_key:
            raise Exception("Please provide claude key")
        return api_key
    if options.model == "custom_api":
        api_key = options.custom_api or env.get("BBM_CUSTOM_API")
        if not api_key:
            raise Exception("Please provide custom translate api")
        return api_key
    if options.model == "gemini":
        return options.gemini_key or env.get("BBM_GOOGLE_GEMINI_KEY")
    if options.model == "groq":
        return options.groq_key or env.get("BBM_GROQ_API_KEY")
    if options.model == "xai":
        return options.xai_key or env.get("BBM_XAI_API_KEY")
    if options.model.startswith("qwen-"):
        return options.qwen_key or env.get("BBM_QWEN_API_KEY")
    return ""


def _split_model_list(raw_value):
    return [item.strip() for item in str(raw_value or "").split(",") if item.strip()]


def configure_loader_from_options(loader, options):
    if options.allow_navigable_strings:
        loader.allow_navigable_strings = True
    if options.translate_tags:
        loader.translate_tags = options.translate_tags
    if options.exclude_translate_tags:
        loader.exclude_translate_tags = options.exclude_translate_tags
    if options.exclude_filelist:
        loader.exclude_filelist = options.exclude_filelist
    if options.only_filelist:
        loader.only_filelist = options.only_filelist
    if options.accumulated_num > 1:
        loader.accumulated_num = options.accumulated_num
    if options.translation_style:
        loader.translation_style = options.translation_style
    if options.batch_size:
        loader.batch_size = options.batch_size
    if options.retranslate:
        loader.retranslate = options.retranslate
    if options.deployment_id:
        assert options.model == "openai", "deployment_id only supports model=openai"
        if not options.api_base:
            raise ValueError("`api_base` must be provided when using `deployment_id`")
        loader.translate_model.set_deployment_id(options.deployment_id)
    if options.model == "openai":
        if options.ollama_model:
            loader.translate_model.set_default_models(ollama_model=options.ollama_model)
        else:
            loader.translate_model.set_model_list(_split_model_list(options.model_list) or OPENAI_DEFAULT_MODEL_LIST)
    if options.model == "groq" and options.model_list:
        loader.translate_model.set_model_list(_split_model_list(options.model_list))
    if options.model.startswith("claude-"):
        loader.translate_model.set_claude_model(options.model)
    if options.model.startswith("qwen-"):
        loader.translate_model.set_qwen_model(options.model)
    if options.block_size > 0:
        loader.block_size = options.block_size
    if options.batch_flag:
        loader.batch_flag = options.batch_flag
    if options.batch_use_flag:
        loader.batch_use_flag = options.batch_use_flag
    if options.model == "gemini":
        loader.translate_model.set_interval(options.interval)
        if options.model_list:
            loader.translate_model.set_model_list(_split_model_list(options.model_list))
        else:
            loader.translate_model.set_geminiflash_models()
