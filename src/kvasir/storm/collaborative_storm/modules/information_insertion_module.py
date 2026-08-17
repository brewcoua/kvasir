import dspy
import logging
import numpy as np
import re

from concurrent.futures import as_completed
from typing import List, Literal, Union, Dict, Optional

from ... import runtime
from ...dataclass import KnowledgeNode, KnowledgeBase
from ...encoder import Encoder, cosine_similarity
from ...interface import Information

logger = logging.getLogger(__name__)


class InsertInformation(dspy.Signature):
    """Your job is to insert the given information to the knowledge base. The knowledge base is a tree based data structure to organize the collection information. Each knowledge node contains information derived from themantically similar question or intent.
    To decide the best placement of the information, you will be navigated in this tree based data structure layer by layer.
    You will be presented with the question and query leads to ththeis information, and tree structure.

    Choose one of:
    - 'insert': to place the information under the current node.
    - 'step': to step into one of the current node's children, named by node_name.
    - 'create': to create a new child node named by node_name and insert the info under it.
    """

    intent: str = dspy.InputField(desc="question and query that lead to this info")
    structure: str = dspy.InputField(desc="tree structure")
    action: Literal["insert", "step", "create"] = dspy.OutputField()
    node_name: str = dspy.OutputField(
        desc="the child node to step into, or the new node to create; empty for 'insert'"
    )


class InsertInformationCandidateChoice(dspy.Signature):
    """Your job is to insert the given information to the knowledge base. The knowledge base is a tree based data structure to organize the collection information. Each knowledge node contains information derived from themantically similar question or intent.
    You will be presented with the question and query leads to this information, and candidate choices of placement. In these choices, -> denotes parent-child relationship. Note that a reasonable placement may not be among these choices.
    """

    intent: str = dspy.InputField(desc="question and query that lead to this info")
    choices: str = dspy.InputField(desc="candidate placements")
    best_placement: Optional[int] = dspy.OutputField(
        desc="the index of the best placement, or null when none of them is reasonable"
    )


class InsertInformationModule(dspy.Module):
    def __init__(self, engine: dspy.LM, encoder: Encoder):
        self.engine = engine
        self.encoder = encoder
        self.insert_info = dspy.ChainOfThought(InsertInformation)
        self.candidate_choosing = dspy.Predict(InsertInformationCandidateChoice)

    def _construct_intent(self, question: str, query: str):
        intent = ""
        if query == "Not applicable":
            return question
        if question:
            intent += f"Question: {question}\n"
        if query:
            intent += f"Query: {query}\n"
        if not intent:
            intent = "Not available."
        return intent

    def _get_navigation_choice(
        self, knowledge_node: KnowledgeNode, question: str, query: str
    ):
        # construct information intent
        intent = self._construct_intent(question, query)
        # construct current kb structure
        structure = f"Current Node: {knowledge_node.name}\n"
        child_names = ", ".join(knowledge_node.get_children_names())
        if child_names:
            structure += f"Child Nodes: {child_names}"
        navigated_path = " -> ".join(knowledge_node.get_path_from_root())
        structure += f"Path you have nagivated: {navigated_path}"

        with dspy.settings.context(lm=self.engine):
            prediction = self.insert_info(intent=intent, structure=structure)

        return prediction.action, prediction.node_name.strip()

    def layer_by_layer_navigation_placement(
        self,
        knowledge_base: KnowledgeBase,
        question: str,
        query: str,
        allow_create_new_node: bool = False,
        root: Optional[KnowledgeNode] = None,
    ):
        current_node: KnowledgeNode = knowledge_base.root if root is None else root

        while True:
            action_type, node_name = self._get_navigation_choice(
                knowledge_node=current_node, question=question, query=query
            )
            if action_type == "insert":
                return dspy.Prediction(
                    information_placement=" -> ".join(
                        current_node.get_path_from_root(root)
                    ),
                    note="None",
                )
            elif action_type == "step":
                for child in current_node.children:
                    if child.name == node_name:
                        current_node = child
                        break
                else:
                    raise ValueError(f"Child node with name {node_name} not found.")
            elif action_type == "create":
                placement_path = current_node.get_path_from_root(root)
                if allow_create_new_node:
                    placement_path.append(node_name)
                    note = f"create new node: {{{node_name}}} under {{{current_node.name}}}"
                else:
                    note = f"attempt to create new node: {{{node_name}}} under {{{current_node.name}}}"
                return dspy.Prediction(
                    information_placement=" -> ".join(placement_path), note=note
                )
            else:
                raise ValueError(f"Unknown action type: {action_type}")

    def _get_sorted_embed_sim_section(
        self,
        encoded_outline: np.ndarray,
        outlines: List[str],
        question: str,
        query: str,
    ):
        if encoded_outline is not None and encoded_outline.size > 0:
            encoded_query = self.encoder.encode(f"{question}, {query}")
            sim = cosine_similarity([encoded_query], encoded_outline)[0]
            sorted_indices = np.argsort(sim)
            sorted_outlines = np.array(outlines)[sorted_indices[::-1]]
            return sorted_outlines
        else:
            return outlines

    def choose_candidate_from_embedding_ranking(
        self,
        question: str,
        query: str,
        encoded_outlines: np.ndarray,
        outlines: List[str],
        top_N_candidates: int = 5,
    ):
        sorted_candidates = self._get_sorted_embed_sim_section(
            encoded_outlines, outlines, question, query
        )
        considered_candidates = sorted_candidates[
            : min(len(sorted_candidates), top_N_candidates)
        ]
        choices_string = "\n".join(
            [
                f"{idx + 1}: {candidate}"
                for idx, candidate in enumerate(considered_candidates)
            ]
        )
        with dspy.settings.context(lm=self.engine):
            best_placement = self.candidate_choosing(
                intent=self._construct_intent(question=question, query=query),
                choices=choices_string,
            ).best_placement
        if best_placement is not None:
            # The choices are numbered from one.
            selected_index = best_placement - 1
            if 0 <= selected_index < len(sorted_candidates):
                return dspy.Prediction(
                    information_placement=sorted_candidates[selected_index],
                    note=f"Choosing from:\n{considered_candidates}",
                )
        return None

    def _info_list_to_intent_mapping(self, information_list: List[Information]):
        intent_to_placement_dict = {}
        for info in information_list:
            intent = (info.meta.get("question", ""), info.meta.get("query", ""))
            if intent not in intent_to_placement_dict:
                intent_to_placement_dict[intent] = None
        return intent_to_placement_dict

    def forward(
        self,
        knowledge_base: KnowledgeBase,
        information: Union[Information, List[Information]],
        allow_create_new_node: bool = False,
        max_thread: int = 5,
        insert_root: Optional[KnowledgeNode] = None,
        skip_candidate_from_embedding: bool = False,
    ):
        if not isinstance(information, List):
            information = [information]
        intent_to_placement_dict: Dict = self._info_list_to_intent_mapping(
            information_list=information
        )

        # process one intent
        def process_intent(question: str, query: str):
            candidate_placement = None
            try:
                if not skip_candidate_from_embedding:
                    candidate_placement = self.choose_candidate_from_embedding_ranking(
                        question=question,
                        query=query,
                        encoded_outlines=encoded_outlines,
                        outlines=outlines,
                        top_N_candidates=8,
                    )
                if candidate_placement is None:
                    candidate_placement = self.layer_by_layer_navigation_placement(
                        knowledge_base=knowledge_base,
                        question=question,
                        query=query,
                        allow_create_new_node=allow_create_new_node,
                        root=insert_root,
                    )
                return (question, query), candidate_placement
            except Exception:
                logger.exception("Failed to place %r in the mind map", question)
                return (question, query), None

        def insert_info_to_kb(info, placement_prediction):
            if placement_prediction is not None:
                missing_node_handling = (
                    "raise error" if not allow_create_new_node else "create"
                )
                knowledge_base.insert_information(
                    path=placement_prediction.information_placement,
                    information=info,
                    missing_node_handling=missing_node_handling,
                    root=insert_root,
                )

        (
            encoded_outlines,
            outlines,
        ) = knowledge_base.get_knowledge_base_structure_embedding(root=insert_root)
        to_return = []
        if not allow_create_new_node:
            # use multi thread as knowledge base structure does not change
            with runtime.ContextThreadPoolExecutor(max_workers=max_thread) as executor:
                futures = {
                    executor.submit(process_intent, question, query): (question, query)
                    for (question, query) in intent_to_placement_dict
                }

                for future in as_completed(futures):
                    (question, query), candidate_placement = future.result()
                    intent_to_placement_dict[(question, query)] = candidate_placement
            # back mapping placement to each information
            for info in information:
                intent = (info.meta.get("question", ""), info.meta.get("query", ""))
                placement_prediction = intent_to_placement_dict.get(intent, None)
                insert_info_to_kb(info, placement_prediction)
                to_return.append((info, placement_prediction))
            return to_return
        else:
            # use sequential insert as knowledge base structure might change
            for question, query in intent_to_placement_dict:
                (
                    encoded_outlines,
                    outlines,
                ) = knowledge_base.get_knowledge_base_structure_embedding(
                    root=insert_root
                )
                _, placement_prediction = process_intent(question=question, query=query)
                intent_to_placement_dict[(question, query)] = placement_prediction

            for info in information:
                intent = (info.meta.get("question", ""), info.meta.get("query", ""))
                placement_prediction = intent_to_placement_dict.get(intent, None)
                insert_info_to_kb(info, placement_prediction)
                to_return.append((info, placement_prediction))
            return to_return


class ExpandSection(dspy.Signature):
    """Your task is to expand a section in the mind map by creating new subsections under the given section.
    You will be given a list of question and query that are used to collect information.
    Each subsection should serve as a coherent and themantic organization of information and corresponding citation numbers. These subsection names are preferred to be concise and precise.
    """

    section: str = dspy.InputField(desc="the section you need to expand")
    info: str = dspy.InputField(desc="the collected information")
    subsections: list[str] = dspy.OutputField(
        desc="the expanded subsection names, or an empty list when the section already "
        "serves as a good organization and needs no expanding"
    )


class ExpandNodeModule(dspy.Module):
    def __init__(
        self,
        engine: dspy.LM,
        information_insert_module: dspy.Module,
        node_expansion_trigger_count: int,
    ):
        self.engine = engine
        self.expand_section = dspy.Predict(ExpandSection)
        self.information_insert_module = information_insert_module
        self.node_expansion_trigger_count = node_expansion_trigger_count

    def _get_cited_info_meta_string(self, node, knowledge_base):
        meta_string = set()
        for index in sorted(list(node.content)):
            info = knowledge_base.info_uuid_to_info_dict[index]
            intent = f"Question: {info.meta['question']}\nQuery: {info.meta['query']}"
            meta_string.add(intent)

        return "\n\n".join(meta_string)

    def _get_expand_subnode_names(self, node, knowledge_base):
        information = self._get_cited_info_meta_string(node, knowledge_base)
        node_path = node.get_path_from_root()
        with dspy.settings.context(lm=self.engine):
            subsections = self.expand_section(
                section=node_path, info=information
            ).subsections
        return [name.strip() for name in subsections if name.strip()]

    def _find_first_node_to_expand(
        self, root: KnowledgeNode, expanded_nodes: List[KnowledgeNode]
    ):
        if root is None:
            return None
        if (
            root not in expanded_nodes
            and len(root.content) >= self.node_expansion_trigger_count
        ):
            return root
        for child in root.children:
            to_return = self._find_first_node_to_expand(
                root=child, expanded_nodes=expanded_nodes
            )
            if to_return is not None:
                return to_return
        return None

    def _expand_node(self, node: KnowledgeNode, knowledge_base: KnowledgeBase):
        subsection_names = self._get_expand_subnode_names(node, knowledge_base)
        if len(subsection_names) <= 1:
            return
        # create new nodes
        for subsection_name in subsection_names:
            # remove citation bracket in the subsection name
            subsection_name = re.sub(r"\[.*?\]", "", subsection_name)
            knowledge_base.insert_node(new_node_name=subsection_name, parent_node=node)
        # reset original information placement
        original_cited_index = node.content
        original_cited_information = [
            knowledge_base.info_uuid_to_info_dict[index]
            for index in original_cited_index
        ]
        node.content = set()
        # re-insert under expanded section
        self.information_insert_module(
            knowledge_base=knowledge_base,
            information=original_cited_information,
            allow_create_new_node=False,
            insert_root=node,
        )

    def forward(self, knowledge_base: KnowledgeBase):
        expanded_nodes = []
        while True:
            node_to_expand = self._find_first_node_to_expand(
                root=knowledge_base.root, expanded_nodes=expanded_nodes
            )
            if node_to_expand is None:
                break
            self._expand_node(node=node_to_expand, knowledge_base=knowledge_base)
            expanded_nodes.append(node_to_expand)
