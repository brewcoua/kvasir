import dspy


class GenerateExpertGeneral(dspy.Signature):
    """You need to select a group of diverse experts who will be suitable to be invited to a roundtable discussion on the given topic.
    Each expert should represent a different perspective, role, or affiliation related to this topic.
    You can use the background information provided about the topic for inspiration. For each expert, add a description of their expertise and what they will focus on during the discussion.
    No need to include speakers name in the output.
    """

    topic: str = dspy.InputField(desc="topic of interest")
    background_info: str = dspy.InputField(desc="background information about the topic")
    topN: int = dspy.InputField(desc="number of speakers needed")
    experts: list[str] = dspy.OutputField(
        desc="one expert per item, written as 'role: short description'"
    )


class GenerateExpertWithFocus(dspy.Signature):
    """
    You need to select a group of speakers who will be suitable to have roundtable discussion on the [topic] of specific [focus].
    You may consider inviting speakers having opposite stands on the topic; speakers representing different interest parties; Ensure that the selected speakers are directly connected to the specific context and scenario provided.
    For example, if the discussion focus is about a recent event at a specific university, consider inviting students, faculty members, journalists covering the event, university officials, and local community members.
    Use the background information provided about the topic for inspiration. For each speaker, add a description of their interests and what they will focus on during the discussion.
    No need to include speakers name in the output.
    """

    topic: str = dspy.InputField(desc="topic of interest")
    background_info: str = dspy.InputField(desc="background information")
    focus: str = dspy.InputField(desc="discussion focus")
    topN: int = dspy.InputField(desc="number of speakers needed")
    experts: list[str] = dspy.OutputField(
        desc="one speaker per item, written as 'role: short description'"
    )


class GenerateExpertModule(dspy.Module):
    def __init__(self, engine: dspy.LM):
        self.engine = engine
        self.generate_expert_general = dspy.Predict(GenerateExpertGeneral)
        self.generate_expert_w_focus = dspy.ChainOfThought(GenerateExpertWithFocus)

    def trim_background(self, background: str, max_words: int = 100):
        words = background.split()
        cur_len = len(words)
        if cur_len <= max_words:
            return background
        trimmed_words = words[: min(cur_len, max_words)]
        trimmed_background = " ".join(trimmed_words)
        return f"{trimmed_background} [rest content omitted]."

    def forward(
        self, topic: str, num_experts: int, background_info: str = "", focus: str = ""
    ):
        with dspy.settings.context(lm=self.engine):
            if not focus:
                experts = self.generate_expert_general(
                    topic=topic, background_info=background_info, topN=num_experts
                ).experts
            else:
                background_info = self.trim_background(
                    background=background_info, max_words=100
                )
                experts = self.generate_expert_w_focus(
                    topic=topic,
                    background_info=background_info,
                    focus=focus,
                    topN=num_experts,
                ).experts
        expert_list = [expert.strip() for expert in experts if expert.strip()]
        return dspy.Prediction(experts=expert_list)
