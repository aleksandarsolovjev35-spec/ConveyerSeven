from vision.overlay.renderers.omission_boundary import draw_omission_item


class LongOmissionRenderer:
    @staticmethod
    def draw_item(img, drawing):
        draw_omission_item(img, drawing)
