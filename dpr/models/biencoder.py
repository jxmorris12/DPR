#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
BiEncoder component + loss function for 'all-in-batch' training
"""

import collections
import logging
import random
from typing import List, Optional, Tuple

import numpy as np
import torch
import torch.nn.functional as F
from torch import Tensor as T
from torch import nn
import tqdm

from dpr.data.biencoder_data import BiEncoderSample
from dpr.utils.data_utils import DEFAULT_SELECTOR, Tensorizer, MultiSetDataIterator
from dpr.utils.model_utils import CheckpointState, move_to_device

logger = logging.getLogger(__name__)

BiEncoderBatch = collections.namedtuple(
    "BiEncoderInput",
    [
        "question_ids",
        "question_segments",
        "context_ids",
        "ctx_segments",
        "is_positive",
        "hard_negatives",
        "encoder_type",
        "query_absolute_idxs", 
        "positive_passage_absolute_idxs",
        "negative_passage_absolute_idxs",
        "hard_negative_passage_absolute_idxs",
    ],
)
# TODO: it is only used by _select_span_with_token. Move them to utils
rnd = random.Random(0)


def dot_product_scores(q_vectors: T, ctx_vectors: T) -> T:
    """
    calculates q->ctx scores for every row in ctx_vector
    :param q_vector:
    :param ctx_vector:
    :return:
    """
    # q_vector: n1 x D, ctx_vectors: n2 x D, result n1 x n2
    r = torch.matmul(q_vectors, torch.transpose(ctx_vectors, 0, 1))
    return r


def cosine_scores(q_vector: T, ctx_vectors: T):
    # q_vector: n1 x D, ctx_vectors: n2 x D, result n1 x n2
    return F.cosine_similarity(q_vector, ctx_vectors, dim=1)


class CoordinateAscentStatus:
    DISABLED = 0
    TRAIN_Q = 1
    TRAIN_CTX = 2


class BiEncoder(nn.Module):
    """Bi-Encoder model component. Encapsulates query/question and context/passage encoders."""

    def __init__(
        self,
        question_model: nn.Module,
        ctx_model: nn.Module,
        fix_q_encoder: bool = False,
        fix_ctx_encoder: bool = False,
        coordinate_ascent_status: bool = CoordinateAscentStatus.DISABLED
    ):
        super(BiEncoder, self).__init__()
        self.question_model = question_model
        self.ctx_model = ctx_model
        self.fix_q_encoder = fix_q_encoder
        self.fix_ctx_encoder = fix_ctx_encoder
        # *** variables used for coordinate ascent ***
        self.coordinate_ascent_status = coordinate_ascent_status
        self.stored_q_vectors = None
        self.stored_ctx_vectors = None
    
    def _precompute_embeddings_full(self, ds_cfg_train_datasets, tensorizer, train_iterator: MultiSetDataIterator,
        num_hard_negatives: int) -> Tuple[List[Optional[T]], List[Optional[T]]]:
        qs = []
        pos_ctxs = []
        neg_ctxs = []

        _shuffle_store = train_iterator.shuffle
        train_iterator.shuffle = False
        # ((TREC) Question Classification dataset contains 5500 labeled questions in training set and another 500 for test set.)


        print(f"precomputing embeddings with {num_hard_negatives} hard negatives")
        
        data = train_iterator.iterate_ds_data(epoch=0)
        for i, samples_batch in tqdm.tqdm(
            enumerate(data),
            colour="red", leave=False,
            desc="Precomputing embeddings",
            total=train_iterator.get_max_iterations()
        ):
            if isinstance(samples_batch, Tuple):
                samples_batch, dataset = samples_batch

            ds_cfg = ds_cfg_train_datasets[dataset]
            special_token = ds_cfg.special_token
            encoder_type = ds_cfg.encoder_type
            shuffle_positives = ds_cfg.shuffle_positives

            biencoder_input = self.create_biencoder_input(
                samples=samples_batch,
                tensorizer=tensorizer,
                insert_title=True,
                num_hard_negatives=num_hard_negatives, # set to 100 to use them all!
                num_other_negatives=0,
                shuffle=False,
                shuffle_positives=False,
                hard_neg_fallback=True,
                query_token=special_token,
            )

            model_device = next(self.parameters()).device

            biencoder_input = BiEncoderBatch(
                **move_to_device(biencoder_input._asdict(), model_device)
            )

            # get the token to be used for representation selection
            selector = ds_cfg.selector if ds_cfg else DEFAULT_SELECTOR

            rep_positions = selector.get_positions(biencoder_input.question_ids, tensorizer)

            q_attn_mask = tensorizer.get_attn_mask(
                biencoder_input.question_ids
            )
            ctx_attn_mask = tensorizer.get_attn_mask(
                biencoder_input.context_ids
            )
            with torch.no_grad():
                local_q_vector, local_ctx_vectors = self(
                    biencoder_input.question_ids,
                    biencoder_input.question_segments,
                    q_attn_mask,
                    biencoder_input.context_ids,
                    biencoder_input.ctx_segments,
                    ctx_attn_mask,
                    encoder_type=encoder_type,
                    representation_token_pos=rep_positions,
                )
            qs.append(local_q_vector)

            # TODO: make this block of code more idiomatic pytorch.
            if (local_ctx_vectors is not None) and len(local_ctx_vectors) > 0:
                ctx_mask = torch.zeros(len(local_ctx_vectors), dtype=torch.bool)
                ctx_mask.scatter_(0, torch.tensor(biencoder_input.is_positive), 1)
                ctx_mask = ctx_mask.to(local_ctx_vectors.device)
                pos_ctxs.append(
                    local_ctx_vectors.masked_select(ctx_mask[:, None]).reshape(-1, 768)
                )
                neg_ctxs.append(
                    local_ctx_vectors.masked_select(~ctx_mask[:, None]).reshape(-1, 768)
                )

        train_iterator.shuffle = True
        # stack context embeddings, with all the positive ones first, so the indices should line up.
        return qs, (pos_ctxs + neg_ctxs)
    
    def _precompute_embeddings_ctx(self, ds_cfg_train_datasets, tensorizer, train_iterator: MultiSetDataIterator, num_hard_negatives: int) -> None:
        print(f"precomputing ctx embeddings with {num_hard_negatives} hard negatives. (how do we actually do this for all possible contexts??)")
        q_emb_list, ctx_emb_list = self._precompute_embeddings_full(
            ds_cfg_train_datasets=ds_cfg_train_datasets,
            tensorizer=tensorizer,
            train_iterator=train_iterator,
            num_hard_negatives=num_hard_negatives
        )
        assert q_emb_list[0] is None
        return torch.cat(ctx_emb_list, dim=0)
    
    def _precompute_embeddings_q(self, ds_cfg_train_datasets, tensorizer, train_iterator: MultiSetDataIterator) -> None:
        q_emb_list, ctx_emb_list = self._precompute_embeddings_full(
            ds_cfg_train_datasets=ds_cfg_train_datasets,
            tensorizer=tensorizer,
            train_iterator=train_iterator,
            num_hard_negatives=0 # no hard negatives for queries
        )
        assert (len(ctx_emb_list) == 0) or (ctx_emb_list[0] is None)
        return torch.cat(q_emb_list, dim=0)
    
    def pre_epoch(self, ds_cfg_train_datasets, tensorizer, train_iterator: MultiSetDataIterator, num_hard_negatives: int) -> None:
        print("BiEncoder pre_epoch() called")
        #
        #  precompute embeddings
        #
        assert self.training # make sure we're not in eval mode
        if self.coordinate_ascent_status == CoordinateAscentStatus.TRAIN_Q:
            self.stored_q_vectors = self._precompute_embeddings_q(
                ds_cfg_train_datasets=ds_cfg_train_datasets,
                tensorizer=tensorizer,
                train_iterator=train_iterator
            )
            print("self.stored_q_vectors.shape =", self.stored_q_vectors.shape)
            self.stored_ctx_vectors = None
        elif self.coordinate_ascent_status == CoordinateAscentStatus.TRAIN_CTX:
            self.stored_ctx_vectors = self._precompute_embeddings_ctx(
                ds_cfg_train_datasets=ds_cfg_train_datasets,
                tensorizer=tensorizer,
                train_iterator=train_iterator,
                num_hard_negatives=num_hard_negatives
            )
            print("self.stored_ctx_vectors.shape =", self.stored_ctx_vectors.shape)
            self.stored_q_vectors = None
        else: # coordinate ascent disabled - do nothing
            pass
        
        # flip switch **after** precomputing embeddings, so that we know
        # we were still in the prev mode so that the model returned None
        # for the other type.
        self._toggle_ca_status()
        print("post epoch self.coordinate_ascent_status =", self.coordinate_ascent_status)
    
    def _toggle_ca_status(self) -> None:
        print("BiEncoder post_epoch() called")
        # 
        #   advance coordinate ascent status
        #
        if self.coordinate_ascent_status == CoordinateAscentStatus.TRAIN_Q:
            #    training query encoder -train ctx next
            self.coordinate_ascent_status = CoordinateAscentStatus.TRAIN_CTX
        elif self.coordinate_ascent_status == CoordinateAscentStatus.TRAIN_CTX:
            #    training context encoder - train query next
            self.coordinate_ascent_status = CoordinateAscentStatus.TRAIN_Q
        else:
            #    coordinate ascent disabled - do nothing
            pass
        print("post epoch self.coordinate_ascent_status =", self.coordinate_ascent_status)
        

    @staticmethod
    def get_representation(
        sub_model: nn.Module,
        ids: T,
        segments: T,
        attn_mask: T,
        fix_encoder: bool = False,
        representation_token_pos=0,
    ) -> (T, T, T):
        sequence_output = None
        pooled_output = None
        hidden_states = None
        if ids is not None:
            if fix_encoder:
                with torch.no_grad():
                    sequence_output, pooled_output, hidden_states = sub_model(
                        ids,
                        segments,
                        attn_mask,
                        representation_token_pos=representation_token_pos,
                    )

                if sub_model.training:
                    sequence_output.requires_grad_(requires_grad=True)
                    pooled_output.requires_grad_(requires_grad=True)
            else:
                sequence_output, pooled_output, hidden_states = sub_model(
                    ids,
                    segments,
                    attn_mask,
                    representation_token_pos=representation_token_pos,
                )

        return sequence_output, pooled_output, hidden_states

    def forward(
        self,
        question_ids: T,
        question_segments: T,
        question_attn_mask: T,
        context_ids: T,
        ctx_segments: T,
        ctx_attn_mask: T,
        encoder_type: str = None,
        representation_token_pos=0,
    ) -> Tuple[T, T]:
        q_encoder = self.question_model if encoder_type is None or encoder_type == "question" else self.ctx_model

        if self.coordinate_ascent_status == CoordinateAscentStatus.TRAIN_CTX:
            q_pooled_out = None
        else:
            _q_seq, q_pooled_out, _q_hidden = self.get_representation(
                q_encoder,
                question_ids,
                question_segments,
                question_attn_mask,
                self.fix_q_encoder,
                representation_token_pos=representation_token_pos,
            )

        if self.coordinate_ascent_status == CoordinateAscentStatus.TRAIN_Q:
            ctx_pooled_out = None
        else:
            ctx_encoder = self.ctx_model if encoder_type is None or encoder_type == "ctx" else self.question_model
            _ctx_seq, ctx_pooled_out, _ctx_hidden = self.get_representation(
                ctx_encoder, context_ids, ctx_segments, ctx_attn_mask, self.fix_ctx_encoder
            )

        return q_pooled_out, ctx_pooled_out

    def create_biencoder_input(
        self,
        samples: List[BiEncoderSample],
        tensorizer: Tensorizer,
        insert_title: bool,
        num_hard_negatives: int = 0,
        num_other_negatives: int = 0,
        shuffle: bool = True,
        shuffle_positives: bool = False,
        hard_neg_fallback: bool = True,
        query_token: str = None,
    ) -> BiEncoderBatch:
        """
        Creates a batch of the biencoder training tuple.
        :param samples: list of BiEncoderSample-s to create the batch for
        :param tensorizer: components to create model input tensors from a text sequence
        :param insert_title: enables title insertion at the beginning of the context sequences
        :param num_hard_negatives: amount of hard negatives per question (taken from samples' pools)
        :param num_other_negatives: amount of other negatives per question (taken from samples' pools)
        :param shuffle: shuffles negative passages pools
        :param shuffle_positives: shuffles positive passages pools
        :return: BiEncoderBatch tuple
        """
        question_tensors = []
        ctx_tensors = []
        positive_ctx_indices = []
        hard_neg_ctx_indices = []

        query_absolute_idxs = []
        positive_passage_absolute_idxs = []
        negative_passage_absolute_idxs = []
        hard_negative_passage_absolute_idxs = []

        for sample in samples:
            # ctx+ & [ctx-] composition
            # as of now, take the first(gold) ctx+ only

            if shuffle and shuffle_positives:
                positive_ctxs = sample.positive_passages
                positive_ctx = positive_ctxs[np.random.choice(len(positive_ctxs))]
            else:
                positive_ctx = sample.positive_passages[0]

            neg_ctxs = sample.negative_passages
            hard_neg_ctxs = sample.hard_negative_passages
            question = sample.query
            # question = normalize_question(sample.query)

            if shuffle:
                random.shuffle(neg_ctxs)
                random.shuffle(hard_neg_ctxs)

            if hard_neg_fallback and len(hard_neg_ctxs) == 0:
                hard_neg_ctxs = neg_ctxs[0:num_hard_negatives]

            neg_ctxs = neg_ctxs[0:num_other_negatives]
            hard_neg_ctxs = hard_neg_ctxs[0:num_hard_negatives]

            all_ctxs = [positive_ctx] + neg_ctxs + hard_neg_ctxs
            hard_negatives_start_idx = 1
            hard_negatives_end_idx = 1 + len(hard_neg_ctxs)

            current_ctxs_len = len(ctx_tensors)

            sample_ctxs_tensors = [
                tensorizer.text_to_tensor(ctx.text, title=ctx.title if (insert_title and ctx.title) else None)
                for ctx in all_ctxs
            ]

            ctx_tensors.extend(sample_ctxs_tensors)
            positive_ctx_indices.append(current_ctxs_len)
            hard_neg_ctx_indices.append(
                [
                    i
                    for i in range(
                        current_ctxs_len + hard_negatives_start_idx,
                        current_ctxs_len + hard_negatives_end_idx,
                    )
                ]
            )

            ########################################################
            query_absolute_idxs.append(sample.query_idx)
            # positive_passage_absolute_idxs.append(
            #     torch.tensor([p.index for p in sample.positive_passages])
            # )
            # negative_passage_absolute_idxs.append(
            #     torch.tensor([p.index for p in sample.negative_passages])
            # )
            # hard_negative_passage_absolute_idxs.append(
            #     torch.tensor([p.index for p in sample.hard_negative_passages])
            # )
            # import pdb; pdb.set_trace()

            ########################################################

            if query_token:
                # TODO: tmp workaround for EL, remove or revise
                if query_token == "[START_ENT]":
                    query_span = _select_span_with_token(question, tensorizer, token_str=query_token)
                    question_tensors.append(query_span)
                else:
                    question_tensors.append(tensorizer.text_to_tensor(" ".join([query_token, question])))
            else:
                question_tensors.append(tensorizer.text_to_tensor(question))

        ctxs_tensor = torch.cat([ctx.view(1, -1) for ctx in ctx_tensors], dim=0)
        questions_tensor = torch.cat([q.view(1, -1) for q in question_tensors], dim=0)

        ctx_segments = torch.zeros_like(ctxs_tensor)
        question_segments = torch.zeros_like(questions_tensor)


        query_absolute_idxs = torch.tensor(query_absolute_idxs)
        # can't stack tensors because they may be different lengths
        # negative_passage_absolute_idxs = torch.stack(
        #     negative_passage_absolute_idxs
        # )
        # hard_negative_passage_absolute_idxs = torch.stack(
        #     hard_negative_passage_absolute_idxs
        # )

        return BiEncoderBatch(
            questions_tensor,
            question_segments,
            ctxs_tensor,
            ctx_segments,
            positive_ctx_indices,
            hard_neg_ctx_indices,
            "question",
            query_absolute_idxs,
            positive_passage_absolute_idxs,
            negative_passage_absolute_idxs,
            hard_negative_passage_absolute_idxs,
        )

    def load_state(self, saved_state: CheckpointState, strict: bool = True):
        # TODO: make a long term HF compatibility fix
        # if "question_model.embeddings.position_ids" in saved_state.model_dict:
        #    del saved_state.model_dict["question_model.embeddings.position_ids"]
        #    del saved_state.model_dict["ctx_model.embeddings.position_ids"]
        self.load_state_dict(saved_state.model_dict, strict=strict)

    def get_state_dict(self):
        return self.state_dict()


class BiEncoderNllLoss(object):
    biencoder: Optional[BiEncoder]
    def __init__(self, biencoder: Optional[BiEncoder] = None):
        self.biencoder = biencoder

    def calc(
        self,
        q_vectors: T,
        ctx_vectors: T,
        positive_idx_per_question: list,
        hard_negative_idx_per_question: list = None,
        absolute_idxs: Optional[T] = None,
        loss_scale: float = None,
    ) -> Tuple[T, int]:
        """
        Computes nll loss for the given lists of question and ctx vectors.
        Note that although hard_negative_idx_per_question in not currently in use, one can use it for the
        loss modifications. For example - weighted NLL with different factors for hard vs regular negatives.
        :return: a tuple of loss value and amount of correct predictions per batch
        """

        if (self.biencoder is None) or (self.biencoder.coordinate_ascent_status == CoordinateAscentStatus.DISABLED):  # regular contrastive loss
            loss, correct_predictions_count = self._calc_contrastive_loss(
                q_vectors=q_vectors,
                ctx_vectors=ctx_vectors,
                positive_idx_per_question=positive_idx_per_question,
            )
        elif self.biencoder.coordinate_ascent_status == CoordinateAscentStatus.TRAIN_Q:
            loss, correct_predictions_count = self._calc_ca_loss(
                batch_vectors=q_vectors,
                stored_vectors=self.biencoder.stored_ctx_vectors,
                absolute_idxs=absolute_idxs,
            )
        elif self.biencoder.coordinate_ascent_status == CoordinateAscentStatus.TRAIN_CTX:
            loss, correct_predictions_count = self._calc_ca_loss(
                batch_vectors=ctx_vectors,
                stored_vectors=self.biencoder.stored_q_vectors,
                absolute_idxs=absolute_idxs,
            )
        else:
            raise ValueError(f'invalid state for biencoder {self.biencoder}')

        if loss_scale:
            loss.mul_(loss_scale)

        return loss, correct_predictions_count
    
    def _calc_ca_loss(
            self,
            batch_vectors: T,
            stored_vectors: T,
            absolute_idxs: T,
            loss_scale: float = None
        ) -> Tuple[T, int]:
        assert stored_vectors is not None, f"got None stored_vectors with coordinate_ascent_status {self.biencoder.coordinate_ascent_status}"
        sims = self.get_scores(batch_vectors, stored_vectors)
        softmax_scores = sims.log_softmax(dim=1)

        # print("absolute_idxs:", absolute_idxs.tolist())
        # print("softmax_scores.shape:", softmax_scores.shape, "absolute_idxs.shape:", absolute_idxs.shape)
        loss = F.nll_loss(
            softmax_scores,
            absolute_idxs.to(softmax_scores.device),
            reduction="mean",
        )

        _max_score, max_idxs = torch.max(softmax_scores, 1)
        correct_predictions_count = (
            max_idxs == absolute_idxs.to(max_idxs.device)
        ).sum()

        return loss, correct_predictions_count

    
    def _calc_contrastive_loss(
            self,
            q_vectors: T,
            ctx_vectors: T,
            positive_idx_per_question: list,
            hard_negative_idx_per_question: list = None,
        ) -> Tuple[T, int]:
        scores = self.get_scores(q_vectors, ctx_vectors)

        if len(q_vectors.size()) > 1:
            q_num = q_vectors.size(0)
            scores = scores.view(q_num, -1)

        softmax_scores = F.log_softmax(scores, dim=1)

        loss = F.nll_loss(
            softmax_scores,
            torch.tensor(positive_idx_per_question).to(softmax_scores.device),
            reduction="mean",
        )

        _max_score, max_idxs = torch.max(softmax_scores, 1)
        correct_predictions_count = (max_idxs == torch.tensor(positive_idx_per_question).to(max_idxs.device)).sum()

        return loss, correct_predictions_count

    @staticmethod
    def get_scores(q_vector: T, ctx_vectors: T) -> T:
        f = BiEncoderNllLoss.get_similarity_function()
        return f(q_vector, ctx_vectors)

    @staticmethod
    def get_similarity_function():
        return dot_product_scores


def _select_span_with_token(text: str, tensorizer: Tensorizer, token_str: str = "[START_ENT]") -> T:
    id = tensorizer.get_token_id(token_str)
    query_tensor = tensorizer.text_to_tensor(text)

    if id not in query_tensor:
        query_tensor_full = tensorizer.text_to_tensor(text, apply_max_len=False)
        token_indexes = (query_tensor_full == id).nonzero()
        if token_indexes.size(0) > 0:
            start_pos = token_indexes[0, 0].item()
            # add some randomization to avoid overfitting to a specific token position

            left_shit = int(tensorizer.max_length / 2)
            rnd_shift = int((rnd.random() - 0.5) * left_shit / 2)
            left_shit += rnd_shift

            query_tensor = query_tensor_full[start_pos - left_shit :]
            cls_id = tensorizer.tokenizer.cls_token_id
            if query_tensor[0] != cls_id:
                query_tensor = torch.cat([torch.tensor([cls_id]), query_tensor], dim=0)

            from dpr.models.reader import _pad_to_len

            query_tensor = _pad_to_len(query_tensor, tensorizer.get_pad_id(), tensorizer.max_length)
            query_tensor[-1] = tensorizer.tokenizer.sep_token_id
            # logger.info('aligned query_tensor %s', query_tensor)

            assert id in query_tensor, "query_tensor={}".format(query_tensor)
            return query_tensor
        else:
            raise RuntimeError("[START_ENT] toke not found for Entity Linking sample query={}".format(text))
    else:
        return query_tensor
