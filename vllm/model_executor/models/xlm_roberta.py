from typing import TYPE_CHECKING, List, Optional, Tuple, Union
import torch

from transformers import XLMRobertaModel as XLMRobertaModelHF
from transformers import XLMRobertaConfig

from vllm.model_executor.input_metadata import InputMetadata
from vllm.config import LoRAConfig
from vllm.model_executor.weight_utils import (default_weight_loader,
                                              hf_model_weights_iterator)
from vllm.model_executor.layers.linear import (
    LinearMethodBase,
    UnquantizedLinearMethod,
)
from tqdm import tqdm

from transformers.modeling_outputs import BaseModelOutputWithPoolingAndCrossAttentions
from vllm.sequence import SamplerOutput
from vllm.model_executor.sampling_metadata import SamplingMetadata


KVCache = Tuple[torch.Tensor, torch.Tensor]

# Faster model init with empty rather than random weights.
def skip(*args, **kwargs):
    pass

torch.nn.init.kaiming_uniform_ = skip
torch.nn.init.uniform_ = skip
torch.nn.init.normal_ = skip

class XLMRobertaModel(torch.nn.Module):
    def __init__(
        self,
        config: XLMRobertaConfig,
        linear_method: Optional[LinearMethodBase] = None,
        lora_config: Optional[LoRAConfig] = None,
    ) -> None:
        super().__init__()
        if linear_method is not None and not isinstance(linear_method, UnquantizedLinearMethod):
            raise NotImplementedError(
                "XLMRobertaModel does not currently support quantization. "
                "Please raise an issue if you would like this feature."
            )
        if lora_config is not None:
            raise NotImplementedError(
                "XLMRobertaModel does not currently support deployments with "
                "LORA Adapters. Please raise an issue if you would like this feature."
            )
        
        self.config = config
        self.linear_method = linear_method
        
        # TODO (Milestone 2): We will implement XLM-RoBERTa with vllm 
        #   layer primitives to not rely on tranformers implementation.
        hf_model = XLMRobertaModelHF(self.config)
        self.embeddings = hf_model.embeddings
        self.encoder = hf_model.encoder
        self.pooler = hf_model.pooler
        

    def load_weights(self,
                     model_name_or_path: str,
                     cache_dir: Optional[str] = None,
                     load_format: str = "auto",
                     revision: Optional[str] = None):
        
        params_dict = dict(self.named_parameters())
        for name, loaded_weight in hf_model_weights_iterator(
            model_name_or_path, cache_dir, load_format, revision):
            param = params_dict[name]
            default_weight_loader(param, loaded_weight)

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[KVCache],
        input_metadata: InputMetadata,
    ) -> torch.Tensor:
        batch_size = 12
        tensor_list = []
        # FIXME: convert the vllm input format into the hf input format
        #   [ Handle batched and non-batched cases ]
        # see https://github.com/huggingface/transformers/blob/v4.39.1/src/transformers/models/xlm_roberta/modeling_xlm_roberta.py#L830C9-L850C90

        for start_index in tqdm(range(0, input_ids.shape[0], batch_size), desc="Inference Embeddings", disable=input_ids.shape[0] < 256):
            ids = input_ids[start_index:start_index+batch_size,:]
            attention_mask =(input_metadata.slot_mapping[start_index:start_index+batch_size,:] != -1).int()
            output_attentions = self.config.output_attentions
            output_hidden_states = self.config.output_hidden_states
            return_dict = self.config.use_return_dict

            if self.config.is_decoder:
                use_cache = self.config.use_cache
            else:
                use_cache = False

            input_shape = ids.size()
            batch_size, seq_length = input_shape
            device = ids.device 
            past_key_values_length = 0

            if attention_mask is None:
                attention_mask = torch.ones(((batch_size, seq_length + past_key_values_length)), device=device)

            if hasattr(self.embeddings, "token_type_ids"):
                buffered_token_type_ids = self.embeddings.token_type_ids[:, :seq_length]
                buffered_token_type_ids_expanded = buffered_token_type_ids.expand(batch_size, seq_length)
                token_type_ids = buffered_token_type_ids_expanded
            else:
                token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=device)

            extended_attention_mask: torch.Tensor = self.get_extended_attention_mask(attention_mask, input_shape)
            encoder_extended_attention_mask = None
            head_mask = self.get_head_mask(None, self.config.num_hidden_layers)

            embedding_output = self.embeddings(
                input_ids=ids,
                position_ids=None,
                token_type_ids=token_type_ids,
                inputs_embeds=None,
                past_key_values_length=past_key_values_length,
            )

            encoder_outputs = self.encoder(
                embedding_output,
                attention_mask=extended_attention_mask,
                head_mask=head_mask,
                encoder_hidden_states=None,
                encoder_attention_mask=encoder_extended_attention_mask,
                past_key_values=None,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )
            sequence_output = encoder_outputs[0]
            pooled_output = self.pooler(sequence_output) if self.pooler is not None else None

            output = BaseModelOutputWithPoolingAndCrossAttentions(
                last_hidden_state=sequence_output,
                pooler_output=pooled_output,
                past_key_values=encoder_outputs.past_key_values,
                hidden_states=encoder_outputs.hidden_states,
                attentions=encoder_outputs.attentions,
                cross_attentions=encoder_outputs.cross_attentions,
            )

            if self.config.name_or_path == "BAAI/bge-m3":
                output = self.dense_embedding(output.last_hidden_state, attention_mask)
                output = torch.nn.functional.normalize(output, dim=-1)
                tensor_list.append(output)
            else:
                tensor_list.append(output)

        return torch.cat(tensor_list, dim=0)

    def get_extended_attention_mask(
        self, attention_mask: torch.Tensor, input_shape: Tuple[int], device: torch.device = None, dtype: torch.float = None
    ) -> torch.Tensor:
        """
        Makes broadcastable attention and causal masks so that future and masked tokens are ignored.

        Arguments:
            attention_mask (`torch.Tensor`):
                Mask with ones indicating tokens to attend to, zeros for tokens to ignore.
            input_shape (`Tuple[int]`):
                The shape of the input to the model.

        Returns:
            `torch.Tensor` The extended attention mask, with a the same dtype as `attention_mask.dtype`.
        """
        if attention_mask.dim() == 3:
            extended_attention_mask = attention_mask[:, None, :, :]
        elif attention_mask.dim() == 2:
            extended_attention_mask = attention_mask[:, None, None, :]
        else:
            raise ValueError(
                f"Wrong shape for input_ids (shape {input_shape}) or attention_mask (shape {attention_mask.shape})"
            )

        dtype = torch.float16
        extended_attention_mask = extended_attention_mask.to(dtype=dtype)  # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * torch.finfo(dtype).min
        return extended_attention_mask

    def get_head_mask(
        self, head_mask: Optional[torch.Tensor], num_hidden_layers: int, is_attention_chunked: bool = False
    ) -> torch.Tensor:
        """
        Prepare the head mask if needed.

        Args:
            head_mask (`torch.Tensor` with shape `[num_heads]` or `[num_hidden_layers x num_heads]`, *optional*):
                The mask indicating if we should keep the heads or not (1.0 for keep, 0.0 for discard).
            num_hidden_layers (`int`):
                The number of hidden layers in the model.
            is_attention_chunked (`bool`, *optional*, defaults to `False`):
                Whether or not the attentions scores are computed by chunks or not.

        Returns:
            `torch.Tensor` with shape `[num_hidden_layers x batch x num_heads x seq_length x seq_length]` or list with
            `[None]` for each layer.
        """
        if head_mask is not None:
            head_mask = self._convert_head_mask_to_5d(head_mask, num_hidden_layers)
            if is_attention_chunked is True:
                head_mask = head_mask.unsqueeze(-1)
        else:
            head_mask = [None] * num_hidden_layers

        return head_mask


    def dense_embedding(self, hidden_state, mask):
        return hidden_state[:, 0]

    def sample(
        self,
        hidden_states: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> Optional[SamplerOutput]:
        return hidden_states