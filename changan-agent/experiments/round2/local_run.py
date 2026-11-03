"""第二版本地基线与LoRA；开发基线达标才训练，固定测试集仅作最终评测。"""

import argparse
import hashlib
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from persistence import read_json, write_json
from experiments.round2.dataset import PREFERENCE, local_messages, score, validate

HERE = Path(__file__).parent


def rates(rows):
    result = {}
    for kind in ("style", "general", "override"):
        selected = [r for r in rows if r["kind"] == kind]
        result[kind] = sum(r["passed"] for r in selected) / len(selected) if selected else None
    content_rows = [r for r in rows if r["kind"] == "style"]
    result["content_coverage"] = sum(r["content_coverage"] for r in content_rows) / len(content_rows) if content_rows else None
    return result


def baseline_gate(evaluations, gate):
    by = {e["condition"]: e["rates"] for e in evaluations}
    return all(by[c]["style"] >= gate["style_min"] and by[c]["general"] >= gate["general_min"]
               and by[c]["general"] >= by["baseline"]["general"] - gate["general_drop_max"]
               and by[c]["override"] >= gate["override_min"] for c in ("prompt", "external"))


def research_gate(evaluations, gate, reload_ok, rollback_ok):
    by = {e["condition"]: e["rates"] for e in evaluations}
    best = max(by[c]["style"] for c in ("prompt", "external"))
    comparisons = {}
    for c in ("lora", "hybrid"):
        comparisons[c] = {"style_gain": by[c]["style"] - best,
                          "general_drop": by["baseline"]["general"] - by[c]["general"],
                          "override": by[c]["override"]}
    return {"conditions": comparisons, "continue_research": bool(reload_ok and rollback_ok and any(
        v["style_gain"] >= gate["style_gain_min"] and v["general_drop"] <= gate["general_drop_max"]
        and v["override"] >= gate["override_min"] for v in comparisons.values())), "deploy": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline-report", type=Path, help="训练所需已通过的独立开发基线报告")
    parser.add_argument("--protocol", type=Path, default=HERE / "protocol.json", help="可选的独立实验协议副本")
    args = parser.parse_args()
    if not 0 <= args.seed <= 2**32-1:
        parser.error("种子无效")
    protocol_path = args.protocol.resolve()
    protocol = read_json(protocol_path)
    config = protocol["lora"]
    data = read_json(HERE / "lora_data.json")
    validate(read_json(HERE / "memory_sessions.json"), data)
    if hashlib.sha256((HERE / "lora_data.json").read_bytes()).hexdigest() != protocol["lora_data_sha256"]:
        raise ValueError("冻结训练数据发生改变")
    if not args.baseline_only:
        if not args.baseline_report:
            parser.error("先执行--baseline-only，再使用--baseline-report指定通过的开发报告")
        baseline = read_json(args.baseline_report)
        if (baseline.get("status") != "completed" or not baseline.get("baseline_passed")
                or baseline.get("data_sha256") != protocol["lora_data_sha256"]
                or baseline.get("protocol") != protocol or baseline.get("code_sha256") != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()):
            parser.error("开发基线未通过或协议/数据/代码不一致，不训练")
    output = args.output or ROOT / "outputs/experiments" / (datetime.now().strftime("%Y-%m-%d_%H%M%S") +
                ("-round2-baseline" if args.baseline_only else f"-round2-lora-seed{args.seed}") + ("-smoke" if args.smoke else ""))
    output.mkdir(parents=True, exist_ok=False)
    for path in (Path(__file__), HERE / "dataset.py", protocol_path, HERE / "lora_data.json"):
        (output / path.name).write_bytes(path.read_bytes())
    report = {"version": 2, "status": "starting", "started": datetime.now().isoformat(), "seed": args.seed,
              "smoke": args.smoke, "protocol": protocol, "data_sha256": protocol["lora_data_sha256"],
              "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "dataset_code_sha256": hashlib.sha256((HERE / "dataset.py").read_bytes()).hexdigest(),
              "baseline_source": str(args.baseline_report) if args.baseline_report else None,
              "evaluations": [], "losses": [], "deploy": False}
    started = time.monotonic()
    write_json(output / "report.json", report)
    try:
        import torch
        import transformers
        import peft
        from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed
        from peft import get_peft_model, PeftModel, LoraConfig
        set_seed(args.seed)
        torch.set_num_threads(4)
        if not torch.cuda.is_available():
            raise RuntimeError("需要已配置的CUDA训练环境")
        device = "cuda"
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float32
        tokenizer = AutoTokenizer.from_pretrained(config["base_model"], revision=config["revision"], trust_remote_code=False, local_files_only=True)
        tokenizer.pad_token = tokenizer.eos_token
        model = AutoModelForCausalLM.from_pretrained(config["base_model"], revision=config["revision"], torch_dtype=dtype,
                                                    trust_remote_code=False, local_files_only=True).to(device)
        report["environment"] = {"python": sys.version, "torch": torch.__version__, "transformers": transformers.__version__,
                                 "peft": peft.__version__, "gpu": torch.cuda.get_device_name(0), "dtype": str(dtype)}

        def generate(model, prompt, condition):
            inputs = tokenizer.apply_chat_template(local_messages(prompt, condition), add_generation_prompt=True, return_tensors="pt").to(device)
            begin = time.monotonic()
            with torch.inference_mode():
                output_ids = model.generate(inputs, attention_mask=torch.ones_like(inputs), do_sample=False, max_new_tokens=config["max_new_tokens"],
                                            pad_token_id=tokenizer.eos_token_id, eos_token_id=tokenizer.eos_token_id, use_cache=True)
            tokens = output_ids[0, inputs.shape[1]:]
            text = tokenizer.decode(tokens, skip_special_tokens=True).strip()
            return text, {"input_tokens": int(inputs.shape[1]), "output_tokens": len(tokens),
                          "truncated": len(tokens) >= config["max_new_tokens"] and int(tokens[-1]) != tokenizer.eos_token_id,
                          "seconds": round(time.monotonic()-begin, 3)}

        split = "dev" if args.baseline_only or args.smoke else "test"
        examples = data[split]
        if args.smoke:
            examples = [next(r for r in examples if r["kind"] == k) for k in ("style", "general", "override")]

        def evaluate(model, condition, disable_adapter=False):
            model.eval()
            def evaluate_rows():
                rows = []
                for item in examples:
                    answer, info = generate(model, item["prompt"], condition)
                    rows.append({"id": item["id"], "kind": item["kind"], "answer": answer, "expected": item["answer"],
                                 "passed": score(item, answer) and not info["truncated"],
                                 "content_coverage": all(word in answer for word in item.get("contains", [])), **info})
                return rows
            if disable_adapter:
                with model.disable_adapter():
                    rows = evaluate_rows()
            else:
                rows = evaluate_rows()
            result = {"condition": condition, "split": split, "rows": rows, "rates": rates(rows)}
            report["evaluations"].append(result)
            write_json(output / "report.json", report)
            print(condition, result["rates"], flush=True)

        if args.baseline_only:
            for condition in ("baseline", "prompt", "external"):
                evaluate(model, condition)
            report["baseline_passed"] = baseline_gate(report["evaluations"], config["baseline_gate"])
            report["status"] = "completed"
            return 0
        model = get_peft_model(model, LoraConfig(r=config["rank"], lora_alpha=config["alpha"],
                   target_modules=["q_proj", "v_proj"], lora_dropout=0, bias="none", task_type="CAUSAL_LM"))
        model.config.use_cache = False
        report["trainable_parameters"] = sum(p.numel() for p in model.parameters() if p.requires_grad)
        train = []
        rng = random.Random(args.seed)
        for row in data["train"]:
            # 所有训练题同时学习无附加偏好与有偏好资料场景，能力保留样例不强套格式。
            for condition in ("baseline", "external"):
                messages = local_messages(row["prompt"], condition)
                prefix = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=True)
                tokens = tokenizer.apply_chat_template(messages + [{"role": "assistant", "content": row["answer"]}], tokenize=True)
                if tokens[:len(prefix)] != prefix or len(tokens) > config["max_length"]:
                    raise ValueError("训练前缀无效或超长，拒绝截断")
                ids = torch.tensor([tokens], device=device)
                labels = ids.clone()
                labels[:, :len(prefix)] = -100
                train.append((ids, labels))
        rng.shuffle(train)
        report["training_examples"] = len(train)
        optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=config["learning_rate"])
        model.train()
        optimizer.zero_grad(set_to_none=True)
        torch.cuda.reset_peak_memory_stats()
        begin = time.monotonic()
        steps = 2 if args.smoke else config["steps"]
        for step in range(steps):
            losses = []
            for micro in range(config["gradient_accumulation"]):
                ids, labels = train[(step * config["gradient_accumulation"] + micro) % len(train)]
                loss = model(input_ids=ids, labels=labels, attention_mask=torch.ones_like(ids)).loss
                if not torch.isfinite(loss):
                    raise ValueError("训练损失非有限值")
                (loss / config["gradient_accumulation"]).backward()
                losses.append(float(loss.detach()))
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)
            report["losses"].append({"step": step+1, "loss": sum(losses)/len(losses)})
            if step == 0 or (step+1) % 20 == 0:
                print("step", step+1, "loss", report["losses"][-1]["loss"], flush=True)
                write_json(output / "report.json", report)
        report["training_seconds"] = round(time.monotonic()-begin, 3)
        report["peak_gpu_allocated_bytes"] = torch.cuda.max_memory_allocated()
        adapter = output / "adapter"
        model.save_pretrained(adapter, safe_serialization=True)
        tokenizer.save_pretrained(output / "tokenizer")
        # Test-set predictions are generated only after all training steps are complete.
        for condition in ("baseline", "prompt", "external"):
            evaluate(model, condition, disable_adapter=True)
        evaluate(model, "lora")
        evaluate(model, "hybrid")
        probes = [next(row for row in examples if row["kind"] == kind) for kind in ("style", "general", "override")]
        rollback_answers, before_reload = [], []
        model.eval()
        with model.disable_adapter():
            for probe in probes:
                rollback_answers.append(generate(model, probe["prompt"], "baseline")[0])
        original = {r["id"]: r["answer"] for r in report["evaluations"][0]["rows"]}
        report["rollback_exact_match"] = all(text == original[p["id"]] for p, text in zip(probes, rollback_answers))
        for probe in probes:
            before_reload.append(generate(model, probe["prompt"], "lora")[0])
        del optimizer, train, model
        torch.cuda.empty_cache()
        base = AutoModelForCausalLM.from_pretrained(config["base_model"], revision=config["revision"], torch_dtype=dtype,
                trust_remote_code=False, local_files_only=True).to(device)
        reloaded = PeftModel.from_pretrained(base, adapter).eval()
        report["reload_exact_match"] = all(generate(reloaded, p["prompt"], "lora")[0] == text for p, text in zip(probes, before_reload))
        report["gate"] = research_gate(report["evaluations"], config["research_gate"], report["reload_exact_match"], report["rollback_exact_match"])
        report["status"] = "completed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = type(exc).__name__ + ": " + str(exc)[:1000]
        print(report["error"], flush=True)
    finally:
        report["seconds"] = round(time.monotonic()-started, 3)
        write_json(output / "report.json", report)
        print(output, flush=True)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
