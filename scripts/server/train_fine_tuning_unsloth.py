#!/usr/bin/env python3
"""
Pipeline de Fine-Tuning Especializado para o Ecossistema Braga Saúde
Modelo Base: Qwen 2.5 7B Instruct (ou 14B) via Unsloth QLoRA 4-bit

Requisitos para Execução:
- GPU com no mínimo 8GB VRAM (Google Colab T4 gratuito, RunPod, Vast.ai ou RTX local)
- pip install unsloth "xformers<0.0.28" "trl<0.9.0" peft accelerate bitsandbytes

Passos automatizados por este script:
1. Carregamento ultrarrápido do Qwen 2.5 4-bit via Unsloth
2. Aplicação de adaptadores LoRA nos módulos de atenção e MLP
3. Carregamento do dataset multicanal sanitizado (dataset_braga.jsonl)
4. Mascaramento de perda (loss masking) focado apenas na resposta do assistente
5. Treinamento supervisionado (SFT)
6. Exportação do adaptador LoRA e conversão direta para GGUF (Q4_K_M) para CPU/Homelab
"""

import os
import argparse
import json

def run_fine_tuning(
    dataset_path: str = "dataset_braga.jsonl",
    model_name: str = "unsloth/Qwen2.5-7B-Instruct-bnb-4bit",
    max_seq_length: int = 2048,
    output_dir: str = "outputs_braga_qwen",
    epochs: int = 3,
    export_gguf: bool = True,
    gguf_quantization: str = "q4_k_m"
):
    print("=" * 60)
    print("🚀 INICIANDO FINE-TUNING DO MODELO BRAGA SAÚDE (QWEN 2.5)")
    print(f"📁 Dataset: {dataset_path}")
    print(f"🧠 Modelo Base: {model_name}")
    print(f"🎯 Saída: {output_dir}")
    print("=" * 60)

    try:
        from unsloth import FastLanguageModel
        from unsloth.chat_templates import get_chat_template, train_on_responses_only
        from datasets import Dataset
        from trl import SFTTrainer, SFTConfig
        import torch
    except ImportError:
        print("\n❌ ERRO: Bibliotecas de fine-tuning não encontradas.")
        print("Instale os pacotes com:")
        print('pip install "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git"')
        print('pip install --no-deps "xformers<0.0.28" trl peft accelerate bitsandbytes')
        return

    # 1. Carregar Modelo e Tokenizer com Unsloth
    print("\n[1/6] Carregando modelo base quantizado em 4-bit...")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=model_name,
        max_seq_length=max_seq_length,
        dtype=None,
        load_in_4bit=True,
    )

    # 2. Configurar Adaptadores LoRA
    print("\n[2/6] Injetando adaptadores LoRA (r=16, alpha=16)...")
    model = FastLanguageModel.get_peft_model(
        model,
        r=16,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        lora_alpha=16,
        lora_dropout=0,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=3407,
        use_rslora=False,
        loftq_config=None,
    )

    # 3. Preparar Chat Template Qwen 2.5
    print("\n[3/6] Configurando template de diálogo ChatML do Qwen 2.5...")
    tokenizer = get_chat_template(
        tokenizer,
        chat_template="qwen-2.5",
        mapping={"role": "role", "content": "content", "user": "user", "assistant": "assistant"}
    )

    # 4. Carregar e Formatar Dataset
    print(f"\n[4/6] Lendo dataset '{dataset_path}'...")
    if not os.path.exists(dataset_path):
        raise FileNotFoundError(f"Arquivo de dataset '{dataset_path}' não encontrado!")

    raw_data = []
    with open(dataset_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if "messages" in item:
                    text = tokenizer.apply_chat_template(item["messages"], tokenize=False, add_generation_prompt=False)
                    raw_data.append({"text": text})
                elif "prompt" in item and "response" in item:
                    msgs = [
                        {"role": "user", "content": item["prompt"]},
                        {"role": "assistant", "content": item["response"]}
                    ]
                    text = tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=False)
                    raw_data.append({"text": text})
            except Exception as e:
                continue

    print(f"Total de conversas válidas carregadas: {len(raw_data)}")
    if len(raw_data) == 0:
        raise ValueError("Nenhum dado válido encontrado para treinamento!")

    dataset = Dataset.from_list(raw_data)

    # 5. Configurar Treinamento SFT
    print("\n[5/6] Configurando SFTTrainer com mascaramento de prompt...")
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        dataset_text_field="text",
        max_seq_length=max_seq_length,
        dataset_num_proc=2,
        packing=False,
        args=SFTConfig(
            per_device_train_batch_size=2,
            gradient_accumulation_steps=4,
            warmup_ratio=0.05,
            num_train_epochs=epochs,
            learning_rate=2e-4,
            fp16=not torch.cuda.is_bf16_supported(),
            bf16=torch.cuda.is_bf16_supported(),
            logging_steps=1,
            optim="adamw_8bit",
            weight_decay=0.01,
            lr_scheduler_type="cosine",
            seed=3407,
            output_dir=output_dir,
            report_to="none"
        ),
    )

    trainer = train_on_responses_only(
        trainer,
        instruction_part="<|im_start|>user\n",
        response_part="<|im_start|>assistant\n",
    )

    print("\n🏁 Iniciando loop de treinamento...")
    trainer_stats = trainer.train()
    print(f"Treinamento concluído com sucesso! Estatísticas: {trainer_stats}")

    lora_dir = os.path.join(output_dir, "lora_adapter")
    print(f"\n[6/6] Salvando adaptador LoRA em '{lora_dir}'...")
    model.save_pretrained(lora_dir)
    tokenizer.save_pretrained(lora_dir)

    if export_gguf:
        gguf_dir = os.path.join(output_dir, f"braga_qwen2.5_7b_{gguf_quantization}")
        print(f"\n📦 Exportando modelo quantizado em formato GGUF ({gguf_quantization}) para '{gguf_dir}'...")
        try:
            model.save_pretrained_gguf(
                gguf_dir,
                tokenizer,
                quantization_method=gguf_quantization
            )
            print(f"🎉 GGUF gerado com sucesso! Arquivo pronto para rodar no llama.cpp / homelab.")
        except Exception as e:
            print(f"⚠️ Aviso na exportação GGUF direta: {e}")
            print("Você pode mesclar os pesos em 16-bit com:")
            print("model.save_pretrained_merged('merged_model', tokenizer, save_method='merged_16bit')")

    print("\n✅ PROCESSO DE FINE-TUNING FINALIZADO COM SUCESSO!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Treinamento Fine-Tuning Qwen 2.5 para Braga Saúde")
    parser.add_argument("--dataset", type=str, default="dataset_braga.jsonl", help="Caminho do arquivo JSONL")
    parser.add_argument("--model", type=str, default="unsloth/Qwen2.5-7B-Instruct-bnb-4bit", help="Modelo Base HF")
    parser.add_argument("--output-dir", type=str, default="outputs_braga_qwen", help="Diretório de saída")
    parser.add_argument("--epochs", type=int, default=3, help="Número de épocas")
    parser.add_argument("--no-gguf", action="store_true", help="Desabilitar exportação GGUF")
    parser.add_argument("--quant", type=str, default="q4_k_m", help="Método de quantização GGUF (ex: q4_k_m, q8_0)")

    args = parser.parse_args()
    run_fine_tuning(
        dataset_path=args.dataset,
        model_name=args.model,
        output_dir=args.output_dir,
        epochs=args.epochs,
        export_gguf=not args.no_gguf,
        gguf_quantization=args.quant
    )
