import torch
import torch.nn.functional as F
from constants import BERT_CLS_TOKEN, BERT_SEP_TOKEN, BERT_PAD_TOKEN

def compute_grads(model, x_embeds, y_labels, create_graph=False):
    outs = model(inputs_embeds=x_embeds, labels=y_labels)
    return torch.autograd.grad(outs.loss, model.parameters(), create_graph=create_graph, allow_unused=True)


def grad_dist(grads1, grads2, args):
    ret = 0.0
    n_g = 0
    for g1, g2 in zip(grads1, grads2):
        if (g1 is not None) and (g2 is not None):
            if args.loss == 'cos':
                ret += 1.0 - (g1 * g2).sum() / (g1.view(-1).norm(p=2) * g2.view(-1).norm(p=2))
            elif args.loss == 'dlg':
                ret += (g1 - g2).square().sum()
            elif args.loss == 'tag':
                ret += (g1 - g2).square().sum() + args.tag_factor * torch.abs(g1 - g2).sum()
            else:
                assert False
            n_g += 1
    if args.loss == 'cos':
        ret /= n_g
    return ret


def get_closest_tokens(inputs_embeds, unused_tokens, embeddings_weight, grads=None, metric='cos'):
    """
    Find tokens that are closest to input embeddings, optionally considering gradient direction.
    
    Args:
        inputs_embeds: The current embedding vectors we're optimizing
        unused_tokens: List of token IDs that should not be considered
        embeddings_weight: The embedding matrix of the vocabulary
        grads: Gradients of the loss with respect to inputs_embeds (optional)
        metric: Distance metric to use ('cos', 'l2', or 'grad_align')
    
    Returns:
        distances: Distance matrix between inputs and vocabulary embeddings
        token_ids: Token IDs with smallest distance (or highest alignment with gradient)
    """
    embeddings_weight = embeddings_weight.repeat(inputs_embeds.shape[0], 1, 1)
    
    if metric == 'l2':
        # Standard L2 distance
        d = torch.cdist(inputs_embeds, embeddings_weight, p=2)
    elif metric == 'cos':
        # Standard cosine similarity
        dp = torch.bmm(inputs_embeds, embeddings_weight.transpose(1, 2))
        norm1 = inputs_embeds.norm(p=2, dim=2).unsqueeze(2)
        norm2 = embeddings_weight.norm(p=2, dim=2).unsqueeze(1)
        d = -dp / (norm1 * norm2)  # Negative because smaller is better
    elif metric == 'grad_align' and grads is not None:
        # Align with gradient descent direction
        # Calculate directions from current embeddings to each token embedding
        directions = embeddings_weight - inputs_embeds.unsqueeze(2)  # [batch, seq_len, vocab_size, embed_dim]
        directions = directions.view(inputs_embeds.shape[0], inputs_embeds.shape[1], -1, inputs_embeds.shape[2])
        
        # Normalize directions
        dir_norms = directions.norm(p=2, dim=3).unsqueeze(3)
        normalized_directions = directions / (dir_norms + 1e-8)
        
        # Normalize gradients (negative for descent direction)
        grad_norms = grads.norm(p=2, dim=2).unsqueeze(2)
        normalized_grads = -grads / (grad_norms + 1e-8)  # Negative for descent direction
        
        # Compute cosine similarity between directions and gradients
        # Higher value means better alignment with gradient descent direction
        alignment = torch.matmul(
            normalized_directions, 
            normalized_grads.unsqueeze(3)
        ).squeeze(3)
        
        # Convert to distance-like metric (smaller is better)
        d = -alignment
    else:
        assert False, f"Unknown metric {metric} or missing gradients for 'grad_align'"

    # Exclude unused tokens by setting their distance to a large value
    d = d.clone()  # Ensure we have a copy to modify
    d[:, :, unused_tokens] = 1e9
    
    # Return distances and indices of minimum distances
    min_distances, token_indices = d.min(dim=2)
    return d, token_indices

def get_reconstruction_loss(model, x_embeds, y_labels, true_grads, args, create_graph=False):
    grads = compute_grads(model, x_embeds, y_labels, create_graph=create_graph)
    return grad_dist(true_grads, grads, args)


def get_perplexity(gpt2, x_embeds, bert_embeddings_weight, gpt2_embeddings_weight, c=0.1):
    gpt2_embeddings_weight = gpt2_embeddings_weight.repeat(x_embeds.shape[0], 1, 1)

    # Get alphas on BERT embeddings --> transfer to GPT-2
    alpha, _ = get_closest_tokens(x_embeds, bert_embeddings_weight)
    # alpha = torch.cdist(x_embeds[:, :-1, :], bert_embeddings_weight, p=2)
    alpha = F.softmax(-alpha/c, dim=2)
    gpt2_embeds = alpha.bmm(gpt2_embeddings_weight)

    # Pass through GPT-2 and get average perplexity
    out_gpt2 = gpt2(inputs_embeds=gpt2_embeds)
    log_probs = out_gpt2.logits.log_softmax(dim=2)
    fuzzy_perplexity = -(log_probs[:, :-1, :] * alpha[:, 1:, :]).sum(dim=2).mean(dim=1).sum()
    return fuzzy_perplexity


def fix_special_tokens(x_embeds, bert_embeddings_weight, pads):
    x_embeds.data[:, 0] = bert_embeddings_weight[BERT_CLS_TOKEN]
    if pads is not None:
        for sen_id in range(x_embeds.shape[0]):
            x_embeds.data[sen_id, pads[sen_id]:] = bert_embeddings_weight[BERT_PAD_TOKEN]
            x_embeds.data[sen_id, pads[sen_id]-1] = bert_embeddings_weight[BERT_SEP_TOKEN]
    elif x_embeds.shape[0] == 1:
        x_embeds.data[:, -1] = bert_embeddings_weight[BERT_SEP_TOKEN]
    return x_embeds


def remove_padding(tokenizer, ids):
    for i in range(ids.shape[0] - 1, -1, -1):
        if ids[i] == BERT_SEP_TOKEN:
            ids = ids[:i+1]
            break
    return tokenizer.decode(ids)
