{ config, ... }:
{
  config = {
    programs.fish = {
      enable = true;
      suppressGreeting = true;
    };

    nmt = {
      description = "if fish.suppressGreeting is set, check fish.config contains the correct setting";
      script = ''
        assertFileContains home-files/.config/fish/config.fish \
          "set fish_greeting"
      '';
    };
  };
}
