class App:
    def get(self, path):
        return lambda fn: fn

    def post(self, path):
        return lambda fn: fn


app = App()
