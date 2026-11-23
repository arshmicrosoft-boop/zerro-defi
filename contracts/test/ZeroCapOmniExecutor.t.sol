// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ZeroCapOmniExecutor} from "../src/ZeroCapOmniExecutor.sol";
import {
    IMorphoLiquidateCallback,
    IMorphoFlashLoanCallback,
    MarketParams
} from "../src/interfaces/IMorphoBlue.sol";
import {ISwapRouter02} from "../src/interfaces/ISwapRouter.sol";
import {IAerodromeRouter} from "../src/interfaces/IAerodromeRouter.sol";

contract MockERC20 {
    string public name;
    string public symbol;
    uint8 public decimals = 18;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function init(string memory _n, string memory _s) external {
        name = _n;
        symbol = _s;
    }

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        require(balanceOf[msg.sender] >= amount, "Insufficient bal");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(balanceOf[from] >= amount, "Insufficient bal");
        require(allowance[from][msg.sender] >= amount, "Insufficient allowance");
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract MockSwapRouter {
    uint256 public rateMultiplierBps = 10500;

    function setRateMultiplierBps(uint256 _bps) external {
        rateMultiplierBps = _bps;
    }

    function exactInputSingle(ISwapRouter02.ExactInputSingleParams calldata params)
        external
        payable
        returns (uint256 amountOut)
    {
        MockERC20(params.tokenIn).transferFrom(msg.sender, address(this), params.amountIn);
        amountOut = (params.amountIn * rateMultiplierBps) / 10000;
        require(amountOut >= params.amountOutMinimum, "Router: slippage exceeded");
        MockERC20(params.tokenOut).mint(params.recipient, amountOut);
    }

    function exactInput(ISwapRouter02.ExactInputParams calldata params)
        external
        payable
        returns (uint256 amountOut)
    {
        bytes calldata path = params.path;
        require(path.length >= 43, "Router: bad path");
        address tokenIn;
        address tokenOut;
        assembly {
            tokenIn := shr(96, calldataload(path.offset))
            tokenOut := shr(96, calldataload(add(path.offset, sub(path.length, 20))))
        }
        MockERC20(tokenIn).transferFrom(msg.sender, address(this), params.amountIn);
        amountOut = (params.amountIn * rateMultiplierBps) / 10000;
        require(amountOut >= params.amountOutMinimum, "Router: slippage exceeded");
        MockERC20(tokenOut).mint(params.recipient, amountOut);
    }
}

contract MockAerodromeRouter {
    uint256 public rateMultiplierBps = 10500;

    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        IAerodromeRouter.Route[] calldata routes,
        address to,
        uint256
    ) external returns (uint256[] memory amounts) {
        require(routes.length >= 1, "Aero: no routes");
        address tokenIn = routes[0].from;
        address tokenOut = routes[routes.length - 1].to;
        MockERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn);
        uint256 out = (amountIn * rateMultiplierBps) / 10000;
        require(out >= amountOutMin, "Aero: slippage exceeded");
        MockERC20(tokenOut).mint(to, out);
        amounts = new uint256[](routes.length + 1);
        amounts[0] = amountIn;
        amounts[routes.length] = out;
    }
}

contract MockFlashTarget {
    function run(address asset, uint256 profit) external {
        MockERC20(asset).mint(msg.sender, profit);
    }
}

contract MockMorpho {
    function liquidate(
        MarketParams calldata params,
        address,
        uint256 seizedAssets,
        uint256 repaidShares,
        bytes calldata data
    ) external returns (uint256, uint256) {
        uint256 collateralToSend = seizedAssets > 0 ? seizedAssets : 100 ether;
        uint256 debtToRepay = repaidShares > 0 ? 100 ether : 95 ether;

        MockERC20(params.collateralToken).mint(msg.sender, collateralToSend);
        IMorphoLiquidateCallback(msg.sender).onMorphoLiquidate(debtToRepay, data);
        MockERC20(params.loanToken).transferFrom(msg.sender, address(this), debtToRepay);
        return (collateralToSend, debtToRepay);
    }

    function flashLoan(address token, uint256 assets, bytes calldata data) external {
        MockERC20(token).mint(msg.sender, assets);
        IMorphoFlashLoanCallback(msg.sender).onMorphoFlashLoan(assets, data);
        MockERC20(token).transferFrom(msg.sender, address(this), assets);
    }
}

contract MockDutchAuction {
    MockERC20 public payToken;
    uint256 public price;
    bool public initialized;

    function init(MockERC20 _payToken, uint256 _price) external {
        payToken = _payToken;
        price = _price;
        initialized = true;
    }

    function buy(address[] calldata assets, address receiver, bytes calldata) external returns (uint256) {
        payToken.transferFrom(msg.sender, address(this), price);
        for (uint256 i = 0; i < assets.length; i++) {
            MockERC20(assets[i]).mint(receiver, 100 ether);
        }
        return price;
    }
}

contract MockBeefyStrategy {
    MockERC20 public weth;
    uint256 public rewardAmount;
    bool public initialized;

    function init(MockERC20 _weth, uint256 _reward) external {
        weth = _weth;
        rewardAmount = _reward;
        initialized = true;
    }

    function harvest(address callFeeRecipient) external {
        weth.mint(callFeeRecipient, rewardAmount);
    }
}

contract ZeroCapOmniExecutorTest {
    ZeroCapOmniExecutor public executor;
    MockERC20 public usdc;
    MockERC20 public weth;
    MockSwapRouter public router;
    MockAerodromeRouter public aeroRouter;
    MockMorpho public morpho;
    MockDutchAuction public auction;
    MockBeefyStrategy public strategy;
    MockFlashTarget public flashTarget;

    address public owner;

    function setUp() public {
        owner = address(this);
        usdc = new MockERC20();
        weth = new MockERC20();
        router = new MockSwapRouter();
        aeroRouter = new MockAerodromeRouter();
        morpho = new MockMorpho();
        auction = new MockDutchAuction();
        strategy = new MockBeefyStrategy();
        flashTarget = new MockFlashTarget();

        usdc.init("USD Coin", "USDC");
        weth.init("Wrapped Ether", "WETH");
        auction.init(usdc, 100 ether);
        strategy.init(weth, 0.01 ether);

        address[] memory initialTargets = new address[](3);
        initialTargets[0] = address(auction);
        initialTargets[1] = address(strategy);
        initialTargets[2] = address(flashTarget);

        executor = new ZeroCapOmniExecutor(
            owner,
            address(morpho),
            address(router),
            address(weth),
            initialTargets
        );
    }

    function testProfitableMorphoLiquidation() public {
        MarketParams memory mp = MarketParams({
            loanToken: address(usdc),
            collateralToken: address(weth),
            oracle: address(0x1111),
            irm: address(0x2222),
            lltv: 860000000000000000
        });

        usdc.mint(address(executor), 50 ether);

        uint256 ownerBefore = usdc.balanceOf(owner);
        uint256 profit = executor.executeMorphoLiquidation(mp, address(0xCAFE), 0, 100 ether, 500, 4 ether);
        require(profit == 5 ether, "Expected 5 ether liquidation profit delta");
        require(usdc.balanceOf(owner) - ownerBefore == 5 ether, "Owner did not receive exact profit delta");
    }

    function testMorphoLiquidationRevertsIfBelowMinProfit() public {
        MarketParams memory mp = MarketParams({
            loanToken: address(usdc),
            collateralToken: address(weth),
            oracle: address(0x1111),
            irm: address(0x2222),
            lltv: 860000000000000000
        });

        (bool ok, ) = address(executor).call(
            abi.encodeWithSelector(
                executor.executeMorphoLiquidation.selector,
                mp,
                address(0xCAFE),
                0,
                100 ether,
                500,
                6 ether
            )
        );
        require(!ok, "Should have reverted on InsufficientProfit");
    }

    function testProfitableDutchAuctionWithSlippageGuards() public {
        address[] memory rewards = new address[](1);
        rewards[0] = address(weth);
        uint24[] memory fees = new uint24[](1);
        fees[0] = 500;
        uint256[] memory minOuts = new uint256[](1);
        minOuts[0] = 104 ether;

        uint256 ownerBefore = usdc.balanceOf(owner);
        uint256 profit = executor.executeDutchAuction(
            address(auction),
            address(usdc),
            100 ether,
            rewards,
            100 ether,
            fees,
            minOuts,
            4 ether
        );
        require(profit == 5 ether, "Expected 5 ether auction profit");
        require(usdc.balanceOf(owner) - ownerBefore == 5 ether, "Owner did not receive auction profit");
    }

    function testProfitableHarvestBountyAndAllowlistGuard() public {
        uint256 ownerBefore = weth.balanceOf(owner);
        uint256 bounty = executor.executeHarvestBounty(address(strategy), 0.005 ether);
        require(bounty == 0.01 ether, "Expected 0.01 WETH bounty");
        require(weth.balanceOf(owner) - ownerBefore == 0.01 ether, "Owner did not receive WETH bounty");

        (bool ok, ) = address(executor).call(
            abi.encodeWithSelector(executor.executeHarvestBounty.selector, address(0xDEAD), 0.001 ether)
        );
        require(!ok, "Unallowed strategy must revert");
    }

    function testRevertOnUnauthorizedCallbacks() public {
        (bool ok1, ) = address(executor).call(
            abi.encodeWithSelector(executor.onMorphoLiquidate.selector, 100 ether, bytes(""))
        );
        require(!ok1, "Direct onMorphoLiquidate call must revert");

        (bool ok2, ) = address(executor).call(
            abi.encodeWithSelector(executor.onMorphoFlashLoan.selector, 100 ether, bytes(""))
        );
        require(!ok2, "Direct onMorphoFlashLoan call must revert");
    }

    function testRoutedMultiHopLiquidation() public {
        MarketParams memory mp = MarketParams({
            loanToken: address(usdc),
            collateralToken: address(weth),
            oracle: address(0x1111),
            irm: address(0x2222),
            lltv: 860000000000000000
        });

        bytes memory path = abi.encodePacked(address(weth), uint24(500), address(usdc));
        address[] memory aeroPath = new address[](0);
        bool[] memory aeroStable = new bool[](0);

        uint256 ownerBefore = usdc.balanceOf(owner);
        uint256 profit = executor.executeMorphoLiquidationRouted(
            mp,
            address(0xCAFE),
            0,
            100 ether,
            1,
            500,
            path,
            aeroPath,
            aeroStable,
            4 ether
        );
        require(profit == 5 ether, "Expected 5 ether routed multi-hop profit");
        require(
            usdc.balanceOf(owner) - ownerBefore == 5 ether,
            "Owner did not receive routed multi-hop profit"
        );
    }

    function testRoutedAerodromeLiquidation() public {
        executor.setAeroRouter(address(aeroRouter), address(0xFAC));

        MarketParams memory mp = MarketParams({
            loanToken: address(usdc),
            collateralToken: address(weth),
            oracle: address(0x1111),
            irm: address(0x2222),
            lltv: 860000000000000000
        });

        address[] memory aeroPath = new address[](2);
        aeroPath[0] = address(weth);
        aeroPath[1] = address(usdc);
        bool[] memory aeroStable = new bool[](1);
        aeroStable[0] = false;

        uint256 ownerBefore = usdc.balanceOf(owner);
        uint256 profit = executor.executeMorphoLiquidationRouted(
            mp,
            address(0xCAFE),
            0,
            100 ether,
            2,
            0,
            bytes(""),
            aeroPath,
            aeroStable,
            4 ether
        );
        require(profit == 5 ether, "Expected 5 ether aerodrome profit");
        require(
            usdc.balanceOf(owner) - ownerBefore == 5 ether,
            "Owner did not receive aerodrome profit"
        );
    }

    function testFlashAction() public {
        bytes memory callData =
            abi.encodeCall(MockFlashTarget.run, (address(usdc), 5 ether));

        uint256 ownerBefore = usdc.balanceOf(owner);
        uint256 profit = executor.executeFlashAction(
            address(usdc),
            100 ether,
            address(flashTarget),
            callData,
            4 ether
        );
        require(profit == 5 ether, "Expected 5 ether flash action profit");
        require(
            usdc.balanceOf(owner) - ownerBefore == 5 ether,
            "Owner did not receive flash action profit"
        );
    }

    function _try(bytes4 sel) internal returns (bool ok) {
        (ok, ) = address(this).call(abi.encodeWithSelector(sel));
    }

    function runSetUp() external returns (uint256) {
        setUp();
        return 1;
    }

    function runOne(uint256 idx) external returns (uint256) {
        setUp();
        bytes4 sel;
        if (idx == 0) sel = this.testProfitableMorphoLiquidation.selector;
        else if (idx == 1) sel = this.testMorphoLiquidationRevertsIfBelowMinProfit.selector;
        else if (idx == 2) sel = this.testProfitableDutchAuctionWithSlippageGuards.selector;
        else if (idx == 3) sel = this.testProfitableHarvestBountyAndAllowlistGuard.selector;
        else if (idx == 4) sel = this.testRevertOnUnauthorizedCallbacks.selector;
        else if (idx == 5) sel = this.testRoutedMultiHopLiquidation.selector;
        else if (idx == 6) sel = this.testRoutedAerodromeLiquidation.selector;
        else if (idx == 7) sel = this.testFlashAction.selector;
        else return 999;
        return _try(sel) ? 1 : 0;
    }

    function runAllTests() external returns (uint256 passedCount) {
        setUp();
        testProfitableMorphoLiquidation();
        passedCount++;

        setUp();
        testMorphoLiquidationRevertsIfBelowMinProfit();
        passedCount++;

        setUp();
        testProfitableDutchAuctionWithSlippageGuards();
        passedCount++;

        setUp();
        testProfitableHarvestBountyAndAllowlistGuard();
        passedCount++;

        setUp();
        testRevertOnUnauthorizedCallbacks();
        passedCount++;

        setUp();
        testRoutedMultiHopLiquidation();
        passedCount++;

        setUp();
        testRoutedAerodromeLiquidation();
        passedCount++;

        setUp();
        testFlashAction();
        passedCount++;
    }
}
