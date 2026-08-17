from typing import Literal

import dspy


from .callback import BaseCallbackHandler
from .collaborative_storm_utils import (
    extract_and_remove_citations,
    keep_first_and_last_paragraph,
)

from .grounded_question_answering import AnswerQuestionModule
from .grounded_question_generation import ConvertUtteranceStyle
from ...dataclass import ConversationTurn
from ...logging_wrapper import LoggingWrapper


# The four contributions an expert can make. Upstream asked for "[type]: [description]" in prose and
# picked the type back out of the answer with a substring scan, which is what "Undefined" meant when
# it failed; the adapter constrains the field instead.
ActionType = Literal[
    "Original Question",
    "Further Details",
    "Information Request",
    "Potential Answer",
]


class GenExpertActionPlanning(dspy.Signature):
    """
    You are an invited speaker in the round table conversation. Your task is to make a very short note to your assistant to help you prepare for your turn in the conversation.
    You will be given the topic we are discussing, your expertise, and the conversation history.
    Take a look at conversation history, especially last few turns, then let your assistant prepare the material for you with one of following ways.
    1. Original Question: Initiates a new question to other speakers.
    2. Further Details: Provides additional information.
    3. Information Request: Requests information from other speakers.
    4. Potential Answer: Offers a possible solution or answer.
    """

    topic: str = dspy.InputField(desc="topic of discussion")
    expert: str = dspy.InputField(desc="who you are invited as")
    summary: str = dspy.InputField(desc="discussion history")
    last_utterance: str = dspy.InputField(desc="last utterance in the conversation")
    action_type: ActionType = dspy.OutputField(desc="how you want to contribute")
    description: str = dspy.OutputField(desc="one sentence description of your note")


class CoStormExpertUtteranceGenerationModule(dspy.Module):
    def __init__(
        self,
        action_planning_lm: dspy.LM,
        utterance_polishing_lm: dspy.LM,
        answer_question_module: AnswerQuestionModule,
        logging_wrapper: LoggingWrapper,
        callback_handler: BaseCallbackHandler = None,
    ):
        self.action_planning_lm = action_planning_lm
        self.utterance_polishing_lm = utterance_polishing_lm
        self.expert_action = dspy.Predict(GenExpertActionPlanning)
        self.change_style = dspy.Predict(ConvertUtteranceStyle)
        self.answer_question_module = answer_question_module
        self.logging_wrapper = logging_wrapper
        self.callback_handler = callback_handler

    def polish_utterance(
        self, conversation_turn: ConversationTurn, last_conv_turn: ConversationTurn
    ):
        # change utterance style
        action_type = conversation_turn.utterance_type
        with self.logging_wrapper.log_event(
            "RoundTableConversationModule.ConvertUtteranceStyle"
        ):
            with dspy.settings.context(
                lm=self.utterance_polishing_lm
            ):
                action_string = (
                    f"{action_type} about: {conversation_turn.claim_to_make}"
                )
                if action_type in ["Original Question", "Information Request"]:
                    action_string = f"{action_type}"
                last_expert_utterance_wo_citation, _ = extract_and_remove_citations(
                    last_conv_turn.utterance
                )
                trimmed_last_expert_utterance = keep_first_and_last_paragraph(
                    last_expert_utterance_wo_citation
                )
                utterance = self.change_style(
                    expert=conversation_turn.role,
                    action=action_string,
                    prev=trimmed_last_expert_utterance,
                    content=conversation_turn.raw_utterance,
                ).utterance
            conversation_turn.utterance = utterance

    def forward(
        self,
        topic: str,
        current_expert: str,
        conversation_summary: str,
        last_conv_turn: ConversationTurn,
    ):
        last_utterance, _ = extract_and_remove_citations(last_conv_turn.utterance)
        if last_conv_turn.utterance_type in [
            "Original Question",
            "Information Request",
        ]:
            action_type = "Potential Answer"
            action_content = last_utterance
        else:
            with self.logging_wrapper.log_event(
                "CoStormExpertUtteranceGenerationModule: GenExpertActionPlanning"
            ):
                with dspy.settings.context(lm=self.action_planning_lm):
                    action = self.expert_action(
                        topic=topic,
                        expert=current_expert,
                        summary=conversation_summary,
                        last_utterance=last_utterance,
                    )
                action_type, action_content = action.action_type, action.description

        if self.callback_handler is not None:
            self.callback_handler.on_expert_action_planning_end()
        # get response
        conversation_turn = ConversationTurn(
            role=current_expert, raw_utterance="", utterance_type=action_type
        )

        if action_type in ["Further Details", "Potential Answer"]:
            with self.logging_wrapper.log_event(
                "RoundTableConversationModule: QuestionAnswering"
            ):
                grounded_answer = self.answer_question_module(
                    topic=topic,
                    question=action_content,
                    mode="brief",
                    style="conversational and concise",
                    callback_handler=self.callback_handler,
                )
            conversation_turn.claim_to_make = action_content
            conversation_turn.raw_utterance = grounded_answer.response
            conversation_turn.queries = grounded_answer.queries
            conversation_turn.raw_retrieved_info = grounded_answer.raw_retrieved_info
            conversation_turn.cited_info = grounded_answer.cited_info
        elif action_type in ["Original Question", "Information Request"]:
            conversation_turn.raw_utterance = action_content

        return dspy.Prediction(conversation_turn=conversation_turn)
