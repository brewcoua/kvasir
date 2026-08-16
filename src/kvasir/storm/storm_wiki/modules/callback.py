class BaseCallbackHandler:
    """Base callback handler that can be used to handle callbacks from the STORM pipeline."""

    def on_identify_perspective_start(self, **kwargs):
        """Run when the perspective identification starts."""
        pass

    def on_identify_perspective_end(self, perspectives: list[str], **kwargs):
        """Run when the perspective identification finishes."""
        pass

    def on_information_gathering_start(self, **kwargs):
        """Run when the information gathering starts."""
        pass

    def on_dialogue_turn_end(self, dlg_turn, **kwargs):
        """Run when a question asking and answering turn finishes."""
        pass

    def on_information_gathering_end(self, **kwargs):
        """Run when the information gathering finishes."""
        pass

    def on_information_organization_start(self, **kwargs):
        """Run when the information organization starts."""
        pass

    def on_direct_outline_generation_end(self, outline: str, **kwargs):
        """Run when the direct outline generation finishes."""
        pass

    def on_outline_refinement_end(self, outline: str, **kwargs):
        """Run when the outline refinement finishes."""
        pass

    def on_article_generation_start(self, sections: list[str], **kwargs):
        """Run when section writing starts, with the sections that will be written."""
        pass

    def on_section_generation_start(self, section: str, **kwargs):
        """Run when one section starts being written.

        Sections are written concurrently, so this is not called from the thread that started the
        run, and start and end are interleaved across sections.
        """
        pass

    def on_section_generation_end(self, section: str, **kwargs):
        """Run when one section finishes being written."""
        pass

    def on_article_generation_end(self, **kwargs):
        """Run when every section has been written."""
        pass

    def on_polish_start(self, **kwargs):
        """Run when article polishing starts."""
        pass

    def on_polish_end(self, **kwargs):
        """Run when article polishing finishes."""
        pass
